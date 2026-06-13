"""
高频数据获取模块 (data_fetcher.py)
===================================
职责：
  1. 通过 OKX V5 公共 WebSocket 实时订阅 USDT 本位永续合约 Ticker 数据
  2. 支持动态增减订阅标的（随盘口状态切换变化）
  3. 双层心跳保活：WebSocket 协议 Ping + OKX 应用层 "ping"/"pong"
  4. 无限指数退避自动重连（含随机抖动）
  5. 逐消息 JSON 解析与字段校验，脏数据自动丢弃

数据流：
  OKX WebSocket → JSON 解析 → 字段校验 → asyncio.Queue → pipeline.py

依赖：
  - websockets >= 13.0
  - config.Settings
"""

import asyncio
import json
import logging
import random
import time
from typing import Optional, Dict, Any, List, Set

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import Settings

logger = logging.getLogger("SwapMomentum.DataFetcher")


class SyntheticEquityFetcher:
    """
    股权代币永续合约数据获取器。

    特性：
      - 连接 OKX 公共 WebSocket，订阅 Ticker 频道
      - 支持动态切换订阅列表（不关闭连接，增量 subscribe/unsubscribe）
      - 双层心跳（协议级 + 应用级 "ping"/"pong"）
      - 指数退避无限重连
      - 全部字段 try-except 保护，单 Tick 损坏不崩溃
    """

    def __init__(self, settings: Settings, data_queue: asyncio.Queue):
        self.settings = settings
        self.data_queue = data_queue

        self._ws: Optional[ClientConnection] = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0.0

        # 当前已订阅的标的集合
        self._subscribed_symbols: Set[str] = set()

        # 运行统计
        self.stats: Dict[str, Any] = {
            "messages_received": 0,
            "messages_parsed": 0,
            "messages_dropped": 0,
            "messages_suppressed": 0,   # 非活跃时段被丢弃的消息
            "reconnect_attempts": 0,
            "subscription_changes": 0,
            "last_price": {},
            "last_update": {},
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        启动数据获取器主循环。

        内部含无限重连，持续运行直到 stop() 被调用。
        """
        self._running = True
        logger.info(f"合约数据获取器启动 | 端点={self.settings.okx_ws_public_url}")

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("数据获取器协程被取消")
                break
            except Exception:
                logger.exception("数据获取器主循环致命异常，将重连")
                if self._running:
                    delay = self._calc_backoff_delay()
                    if self._running:
                        logger.info(f"退避 {delay:.1f}s 后重连（第{self._reconnect_count}次）")
                        await asyncio.sleep(delay)

        logger.info("数据获取器主循环已退出")

    async def stop(self) -> None:
        """优雅停止。"""
        if not self._running:
            return
        logger.info("正在停止数据获取器...")
        self._running = False

        if self._ws is not None:
            try:
                await self._ws.close()
                logger.info("WebSocket 连接已关闭")
            except Exception:
                logger.warning("关闭 WebSocket 时异常（可忽略）")

        try:
            self.data_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def update_subscriptions(self, active_symbols: List[str]) -> None:
        """
        动态切换订阅列表。

        对比当前已订阅集合，取消不再需要的标的，
        新增需要监控的标的。已在列表中且仍需要的保持不变。

        Args:
            active_symbols: 新的活跃标的列表（空列表 = 取消全部）
        """
        if self._ws is None:
            # 连接尚未建立，记录待订阅列表，连接后补订
            self._subscribed_symbols = set(active_symbols)
            return

        new_set = set(active_symbols)
        old_set = self._subscribed_symbols

        to_remove = old_set - new_set
        to_add = new_set - old_set

        for sym in to_remove:
            try:
                unsub_msg = {
                    "op": "unsubscribe",
                    "args": [{"channel": "tickers", "instId": sym}],
                }
                await self._ws.send(json.dumps(unsub_msg))
                logger.info(f"取消订阅: {sym}")
                self.stats["subscription_changes"] += 1
            except Exception:
                logger.exception(f"取消订阅 {sym} 失败")

        for sym in to_add:
            try:
                sub_msg = {
                    "op": "subscribe",
                    "args": [{"channel": "tickers", "instId": sym}],
                }
                await self._ws.send(json.dumps(sub_msg))
                logger.info(f"新增订阅: {sym}")
                self.stats["subscription_changes"] += 1
            except Exception:
                logger.exception(f"新增订阅 {sym} 失败")

        self._subscribed_symbols = new_set

    # ------------------------------------------------------------------
    # 内部：连接与监听
    # ------------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        """建立连接、订阅、监听消息。"""
        self._last_message_time = time.time()

        try:
            logger.info(f"正在连接 → {self.settings.okx_ws_public_url}")

            async with websockets.connect(
                self.settings.okx_ws_public_url,
                ping_interval=self.settings.ws_ping_interval,
                ping_timeout=self.settings.ws_ping_timeout,
                close_timeout=5,
                max_size=2 ** 20,
            ) as ws:
                self._ws = ws
                self._reconnect_count = 0
                self.stats["reconnect_attempts"] = 0
                logger.info(f"WebSocket 连接成功 | 远端={ws.remote_address}")

                # 订阅之前记录的标的列表（可能是 update_subscriptions 在连接前设置的）
                if self._subscribed_symbols:
                    await self._batch_subscribe(list(self._subscribed_symbols))

                heartbeat_task = asyncio.create_task(self._heartbeat_monitor())

                try:
                    await self._message_loop(ws)
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

        except asyncio.CancelledError:
            raise
        except ConnectionClosed as e:
            logger.warning(f"WebSocket 连接关闭 | code={e.code} reason={e.reason!r}")
        except (OSError, WebSocketException) as e:
            logger.error(f"WebSocket 网络异常 | type={type(e).__name__} detail={e}")
        except Exception:
            logger.exception("连接监听未预期异常")
        finally:
            self._ws = None

    async def _batch_subscribe(self, symbols: List[str]) -> None:
        """批量订阅 Ticker 频道。"""
        if not symbols:
            return

        args = [{"channel": "tickers", "instId": sym} for sym in symbols]
        subscribe_msg = {"op": "subscribe", "args": args}

        try:
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info(f"批量订阅 {len(symbols)} 个标的: {symbols}")
        except Exception:
            logger.exception("批量订阅失败")
            raise

    async def _heartbeat_monitor(self) -> None:
        """
        连接健康度监控 + OKX 应用层主动心跳保活。

        OKX V5 要求客户端必须定时发送字符串 "ping"，
        否则即使底层 WebSocket 协议级保活正常，
        也会被应用层网关强行切断。
        """
        while self._running and self._ws is not None:
            try:
                await asyncio.sleep(self.settings.ws_ping_interval)
                if not self._running or self._ws is None:
                    break

                # ── 主动发送 OKX 应用层心跳 "ping" ──
                try:
                    await self._ws.send("ping")
                    logger.debug("应用层心跳 ping 已发送")
                except Exception:
                    logger.warning("发送应用层 ping 失败（连接可能已断开）")

                # ── 被动静默检测 ──
                elapsed = time.time() - self._last_message_time
                if elapsed > self.settings.ws_ping_interval * 2.5:
                    logger.warning(f"数据静默 {elapsed:.0f}s，连接可能僵死")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _message_loop(self, ws: ClientConnection) -> None:
        """消息监听主循环。"""
        logger.info("开始监听行情推送...")

        async for raw_message in ws:
            if not self._running:
                break

            self._last_message_time = time.time()
            self.stats["messages_received"] += 1

            try:
                await self._handle_message(raw_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["messages_dropped"] += 1

        logger.info("WebSocket 消息流已结束")

    # ------------------------------------------------------------------
    # 内部：消息处理
    # ------------------------------------------------------------------

    async def _handle_message(self, raw_message: str) -> None:
        """处理单条原始消息。"""
        # OKX 应用层 Ping/Pong —— 纯文本协议，不参与 JSON 解析
        if raw_message == "ping":
            if self._ws is not None:
                try:
                    await self._ws.send("pong")
                except Exception:
                    pass
            return
        if raw_message == "pong":
            return

        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败 | raw(len={len(raw_message)})={raw_message[:200]}")
            return

        event = msg.get("event", "")

        if event == "subscribe":
            arg = msg.get("arg", {})
            logger.info(f"订阅确认 | channel={arg.get('channel')} instId={arg.get('instId')}")
            return
        if event == "unsubscribe":
            arg = msg.get("arg", {})
            logger.info(f"取消订阅确认 | instId={arg.get('instId')}")
            return
        if event == "error":
            logger.error(f"服务端错误 | code={msg.get('code')} msg={msg.get('msg')}")
            return

        data = msg.get("data")
        if not data or not isinstance(data, list) or len(data) == 0:
            return

        self._handle_ticker_data(data[0])

    def _handle_ticker_data(self, ticker: dict) -> None:
        """解析 Ticker 数据并放入队列。"""
        try:
            symbol = ticker.get("instId", "")
            price = float(ticker["last"])
            last_size = float(ticker.get("lastSz", 0))
            volume_usdt = last_size * price
            vol_quote_24h = float(ticker.get("volCcy24h", 0))
            vol_base_24h = float(ticker.get("vol24h", 0))
            ask = float(ticker.get("askPx", 0))
            bid = float(ticker.get("bidPx", 0))
            ts_raw = ticker.get("ts", 0)
            server_ts = int(ts_raw) / 1000.0 if ts_raw else 0.0
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Ticker 字段解析失败 | error={e} | raw_keys={list(ticker.keys())}")
            self.stats["messages_dropped"] += 1
            return

        if price <= 0:
            self.stats["messages_dropped"] += 1
            return

        parsed = {
            "symbol": symbol,
            "price": price,
            "last_trade_size": last_size,
            "volume_usdt": volume_usdt,
            "volume_quote_24h": vol_quote_24h,
            "volume_base_24h": vol_base_24h,
            "ask": ask,
            "bid": bid,
            "spread": ask - bid if ask > 0 and bid > 0 else 0.0,
            "server_timestamp": server_ts,
            "local_timestamp": time.time(),
        }

        self.stats["last_price"][symbol] = price
        self.stats["last_update"][symbol] = parsed["local_timestamp"]
        self.stats["messages_parsed"] += 1

        try:
            self.data_queue.put_nowait(parsed)
        except asyncio.QueueFull:
            self.stats["messages_dropped"] += 1

    # ------------------------------------------------------------------
    # 内部：重连退避
    # ------------------------------------------------------------------

    def _calc_backoff_delay(self) -> float:
        """指数退避延迟计算。"""
        self._reconnect_count += 1
        self.stats["reconnect_attempts"] += 1

        max_attempts = self.settings.ws_max_reconnect_attempts
        if max_attempts > 0 and self._reconnect_count > max_attempts:
            logger.critical(f"最大重连次数达到 {max_attempts}，停止运行")
            self._running = False
            return 0.0

        delay = self.settings.ws_reconnect_min_delay * (
            self.settings.ws_reconnect_backoff_factor ** self._reconnect_count
        )
        delay = min(delay, self.settings.ws_reconnect_max_delay)
        delay *= 0.5 + random.random()
        return delay
