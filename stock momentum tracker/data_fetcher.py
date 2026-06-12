"""
数据获取模块 (data_fetcher.py)
===============================
职责：
  1. 统一接口同时获取美股（Alpaca REST / yfinance）和韩股（yfinance）实时行情
  2. 优先使用 Alpaca Markets API（高精度、低延迟）
  3. 无 API Key 时自动降级为 yfinance（免费但 15-20 分钟延迟）
  4. 处理股市特有异常：停牌（无数据返回）、非交易时段空返回、网络波动
  5. 带指数退避的断线重连机制

数据流：
  Alpaca REST / yfinance → 解析归 → asyncio.Queue → analytics.py

依赖：
  - alpaca-py（可选，需要 API Key）
  - yfinance（免费备选）
  - asyncio.to_thread() 包裹所有同步 HTTP 调用
"""

import asyncio
import json
import logging
import random
import time
from typing import Optional, Dict, Any, List, Set

from config import Settings
from session_manager import SessionState

logger = logging.getLogger("StockMomentum.DataFetcher")


# ============================================================================
# 股票数据获取器
# ============================================================================

class EquityDataFetcher:
    """
    多数据源股票行情获取器。

    支持三种获取模式：
      1. Alpaca REST API  → 美股实时（需 API Key，延迟 <100ms）
      2. yfinance REST     → 通用备选（免费，延迟 15-20 分钟）
      3. 自动降级          → 无 API Key 时自动使用 yfinance

    韩股始终使用 yfinance（Alpaca 不覆盖 KRX）。

    所有 I/O 操作通过 asyncio.to_thread() 分流到线程池，
    绝不阻塞主事件循环。
    """

    def __init__(self, settings: Settings, data_queue: asyncio.Queue):
        """
        初始化数据获取器。

        Args:
            settings:   系统配置
            data_queue: 异步队列，推送解析后的行情数据
        """
        self.settings = settings
        self.data_queue = data_queue

        # Alpaca 客户端（延迟初始化）
        self._alpaca_client = None

        # yfinance ticker 缓存
        self._yf_tickers: Dict[str, Any] = {}

        # 运行状态
        self._running = False
        self._current_symbols: Set[str] = set()
        self._poll_tasks: Dict[str, asyncio.Task] = {}

        # 统计
        self.stats: Dict[str, Any] = {
            "messages_received": 0,
            "messages_parsed": 0,
            "messages_dropped": 0,
            "alpaca_requests": 0,
            "yf_requests": 0,
            "connection_errors": 0,
            "last_price": {},
            "last_update": {},
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动数据获取器主循环。持续运行直到 stop() 被调用。"""
        self._running = True
        logger.info("股票数据获取器已启动")

        while self._running:
            try:
                # 实际的数据轮询由 external 调度（通过 update_symbols 切换标的）
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("数据获取器主循环异常")
                await asyncio.sleep(1)

        logger.info("股票数据获取器已退出")

    async def stop(self) -> None:
        """停止数据获取器，关闭所有轮询任务。"""
        self._running = False

        for symbol, task in list(self._poll_tasks.items()):
            task.cancel()
        self._poll_tasks.clear()

        # 毒丸
        try:
            self.data_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        logger.info("数据获取器已停止，所有轮询任务已取消")

    async def update_symbols(self, symbols: List[str], session_state: SessionState) -> None:
        """
        动态切换当前监控的标的列表。

        当盘口状态切换时由 pipeline.py 调用。
        停止不再需要的轮询任务，启动新标的的轮询。

        Args:
            symbols:       新的标的代码列表（空列表表示休市）
            session_state: 当前盘口状态
        """
        new_set = set(symbols)
        old_set = self._current_symbols

        # 停止已移除的标的的轮询
        removed = old_set - new_set
        for sym in removed:
            if sym in self._poll_tasks:
                self._poll_tasks[sym].cancel()
                del self._poll_tasks[sym]
                logger.debug(f"已停止 {sym} 数据轮询")

        # 启动新加入的标的的轮询
        added = new_set - old_set
        for sym in added:
            task = asyncio.create_task(
                self._poll_symbol(sym, session_state),
                name=f"poll_{sym}",
            )
            self._poll_tasks[sym] = task
            logger.info(f"已启动 {sym} 数据轮询")

        self._current_symbols = new_set

        if added or removed:
            logger.info(
                f"标的池更新 | 新增={list(added)} | 移除={list(removed)} | "
                f"当前活跃={list(self._current_symbols)}"
            )

    # ------------------------------------------------------------------
    # 内部：单标的数据轮询
    # ------------------------------------------------------------------

    async def _poll_symbol(self, symbol: str, session_state: SessionState) -> None:
        """
        轮询单个标的的实时行情。

        根据标的类型和 API 配置选择数据源：
          - 美股 + Alpaca 已配置 → Alpaca REST
          - 其他 → yfinance

        带指数退避的错误重试。
        """
        error_count = 0

        while self._running and symbol in self._current_symbols:
            try:
                if self._is_us_symbol(symbol) and self.settings.alpaca_configured:
                    data = await self._fetch_alpaca_quote(symbol)
                else:
                    data = await self._fetch_yfinance_quote(symbol)

                if data:
                    self.stats["messages_received"] += 1
                    self.stats["messages_parsed"] += 1
                    self.stats["last_price"][symbol] = data["price"]
                    self.stats["last_update"][symbol] = data["local_timestamp"]

                    try:
                        self.data_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        self.stats["messages_dropped"] += 1
                        logger.warning("数据队列已满，丢弃当前消息")

                error_count = 0  # 成功后重置

            except asyncio.CancelledError:
                break
            except Exception as e:
                error_count += 1
                self.stats["connection_errors"] += 1
                delay = min(1.0 * (2 ** error_count), 60)
                delay *= 0.5 + random.random()
                logger.warning(
                    f"{symbol} 数据获取失败 (连续{error_count}次) | "
                    f"error={e} | 退避={delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue

            # 轮询间隔
            interval = self.settings.yf_poll_interval
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # 内部：Alpaca 数据获取（美股实时）
    # ------------------------------------------------------------------

    def _get_alpaca_client(self):
        """延迟初始化 Alpaca 客户端。"""
        if self._alpaca_client is None and self.settings.alpaca_configured:
            try:
                from alpaca.data.historical import StockHistoricalDataClient

                if self.settings.alpaca_use_paper:
                    base_url = "https://paper-api.alpaca.markets"
                else:
                    base_url = "https://api.alpaca.markets"

                self._alpaca_client = StockHistoricalDataClient(
                    api_key=self.settings.alpaca_api_key,
                    secret_key=self.settings.alpaca_api_secret,
                    url_override=base_url,
                )
                logger.info("Alpaca 客户端初始化成功")
            except ImportError:
                logger.error("alpaca-py 未安装，降级为 yfinance")
                self._alpaca_client = None
            except Exception:
                logger.exception("Alpaca 客户端初始化失败")
                self._alpaca_client = None
        return self._alpaca_client

    async def _fetch_alpaca_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        通过 Alpaca REST API 获取单只美股的最新报价。

        使用 StockLatestQuoteRequest 获取最新 bid/ask/price，
        然后通过 StockLatestTradeRequest 获取最新成交价。

        Args:
            symbol: 美股代码

        Returns:
            标准化的行情数据 dict，获取失败返回 None
        """
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

        client = self._get_alpaca_client()
        if client is None:
            return await self._fetch_yfinance_quote(symbol)

        try:
            # 并行获取报价和成交
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            trade_req = StockLatestTradeRequest(symbol_or_symbols=[symbol])

            quote_future = asyncio.to_thread(client.get_stock_latest_quote, quote_req)
            trade_future = asyncio.to_thread(client.get_stock_latest_trade, trade_req)

            quote_resp, trade_resp = await asyncio.gather(
                quote_future, trade_future, return_exceptions=True
            )

            if isinstance(quote_resp, Exception):
                logger.warning(f"Alpaca {symbol} 报价获取失败: {quote_resp}")
                quote_resp = None
            if isinstance(trade_resp, Exception):
                logger.warning(f"Alpaca {symbol} 成交获取失败: {trade_resp}")
                trade_resp = None

            # 解析价格
            price = 0.0
            bid = 0.0
            ask = 0.0
            volume = 0.0

            if trade_resp and symbol in trade_resp:
                trade = trade_resp[symbol]
                price = float(trade.price) if hasattr(trade, 'price') else 0.0
                volume = float(trade.size) if hasattr(trade, 'size') else 0.0

            if quote_resp and symbol in quote_resp:
                quote = quote_resp[symbol]
                bid = float(quote.bid_price) if hasattr(quote, 'bid_price') else 0.0
                ask = float(quote.ask_price) if hasattr(quote, 'ask_price') else 0.0
                if price == 0.0 and bid > 0 and ask > 0:
                    price = (bid + ask) / 2.0

            if price <= 0:
                logger.debug(f"Alpaca {symbol}: 无有效价格，跳过")
                return None

            self.stats["alpaca_requests"] += 1

            return {
                "symbol": symbol,
                "price": price,
                "volume": volume,
                "volume_usdt": volume * price,
                "bid": bid,
                "ask": ask,
                "spread": ask - bid if ask > 0 and bid > 0 else 0.0,
                "source": "alpaca",
                "server_timestamp": time.time(),
                "local_timestamp": time.time(),
            }

        except Exception:
            logger.exception(f"Alpaca {symbol} 数据获取异常")
            return None

    # ------------------------------------------------------------------
    # 内部：yfinance 数据获取（通用备选）
    # ------------------------------------------------------------------

    async def _fetch_yfinance_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        通过 yfinance 获取单只股票的最新行情。

        优先使用 ticker.fast_info（快速），
        失败时降级为 ticker.history（更可靠但较慢）。

        Args:
            symbol: 股票代码（美股：AAPL，韩股：005930.KS）

        Returns:
            标准化的行情数据 dict，获取失败返回 None
        """
        try:
            import yfinance as yf

            if symbol not in self._yf_tickers:
                self._yf_tickers[symbol] = yf.Ticker(symbol)

            ticker = self._yf_tickers[symbol]

            # 在线程池中执行（yfinance 全部是同步调用）
            def _get_data():
                price = 0.0
                volume = 0.0
                bid = 0.0
                ask = 0.0

                try:
                    info = ticker.fast_info
                    price = float(
                        info.get("last_price")
                        or info.get("regular_market_price")
                        or info.get("current_price")
                        or 0.0
                    )
                    volume_val = info.get("last_volume") or info.get("regular_market_volume") or 0
                    # Alpaca volume size is int, yfinance might be float
                    if hasattr(volume_val, 'item'):
                        volume_val = volume_val.item()
                    volume = float(volume_val) if volume_val else 0.0
                except Exception:
                    pass

                # 如果 fast_info 失败，尝试 history
                if price <= 0:
                    try:
                        df = ticker.history(period="1d", interval="1m")
                        if not df.empty:
                            last = df.iloc[-1]
                            price = float(last["Close"])
                            volume = float(last["Volume"]) if "Volume" in last else 0.0
                    except Exception:
                        pass

                # 尝试获取 bid/ask
                try:
                    info_full = ticker.info if hasattr(ticker, 'info') else {}
                    bid = float(info_full.get("bid", 0) or 0)
                    ask = float(info_full.get("ask", 0) or 0)
                except Exception:
                    pass

                return price, volume, bid, ask

            price, volume, bid, ask = await asyncio.to_thread(_get_data)

            if price <= 0:
                logger.debug(f"yfinance {symbol}: 无有效价格数据")
                return None

            self.stats["yf_requests"] += 1

            return {
                "symbol": symbol,
                "price": price,
                "volume": volume,
                "volume_usdt": volume * price,
                "bid": bid,
                "ask": ask,
                "spread": ask - bid if ask > 0 and bid > 0 else 0.0,
                "source": "yfinance",
                "server_timestamp": time.time(),
                "local_timestamp": time.time(),
            }

        except ImportError:
            logger.error("yfinance 未安装，无法获取数据")
            return None
        except Exception:
            logger.exception(f"yfinance {symbol} 数据获取异常")
            return None

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _is_us_symbol(self, symbol: str) -> bool:
        """判断是否为美股代码（不含 .KS/.KQ 等后缀）。"""
        return "." not in symbol

    @property
    def active_symbols(self) -> List[str]:
        """当前活跃的标的列表"""
        return list(self._current_symbols)
