"""
历史回测数据源模块 (backtest_feeder.py)
=========================================
职责：
  1. 读取本地历史 K 线或 Tick CSV 文件，解析为与实盘完全相同的字典结构
  2. 对外接口 (start/stop/update_subscriptions) 与 SyntheticEquityFetcher 一致
     实现无缝依赖注入——在 pipeline.py 中可直接替换数据源
  3. 速率控制 (Playback Speed)：光速回放 / 仿真时间流逝

CSV 文件格式要求：
  必须包含以下列（大小写不敏感）：
    timestamp       → 时间戳 (UNIX 秒或 ISO 8601 字符串)
    symbol          → 合约代码，如 "TSLA-USDT-SWAP"
    price           → 成交价
    volume_usdt     → 成交额 (USDT) 或 volume (股数×价格)
    last_trade_size → 成交数量（可选，默认 0）

  可选列：
    volume_quote_24h, volume_base_24h, ask, bid, server_timestamp
"""

import asyncio
import csv
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

from config import Settings

logger = logging.getLogger("SwapMomentum.BacktestFeeder")


# ============================================================================
# 历史 CSV 数据源
# ============================================================================

class HistoricalCSVFeeder:
    """
    历史数据回放器——接口完全兼容 SyntheticEquityFetcher。

    使用方法（依赖注入）：
        # 实盘
        fetcher = SyntheticEquityFetcher(settings, data_queue)

        # 回测（直接替换）
        fetcher = HistoricalCSVFeeder(
            settings=settings,
            data_queue=data_queue,
            csv_path="data/BTC_USDT_2025.csv",
            playback_speed=10.0,   # 10 倍速
        )

    CSV 示例：
        timestamp,symbol,price,volume_usdt
        1735689600.0,TSLA-USDT-SWAP,248.50,12500.0
        1735689600.5,TSLA-USDT-SWAP,248.52,8300.0
    """

    def __init__(
        self,
        settings: Settings,
        data_queue: asyncio.Queue,
        csv_path: str,
        playback_speed: float = 1.0,
    ):
        """
        初始化历史数据回放器。

        Args:
            settings:       系统配置
            data_queue:     异步队列（与实盘共用）
            csv_path:       CSV 文件路径
            playback_speed: 播放速率
                            0   = 光速（不等待，全速推入队列）
                            1.0 = 实时（按原始 Δt）
                            10  = 10 倍速
        """
        self.settings = settings
        self.data_queue = data_queue
        self.csv_path = Path(csv_path)
        self.playback_speed = playback_speed

        self._running = False
        self._current_symbols: Set[str] = set()
        self._messages_pushed = 0
        self._start_time = 0.0

        # 模拟实盘的运行统计
        self.stats: Dict[str, Any] = {
            "messages_received": 0,
            "messages_parsed": 0,
            "messages_dropped": 0,
            "reconnect_attempts": 0,
            "subscription_changes": 0,
            "last_price": {},
            "last_update": {},
            "csv_rows_total": 0,
        }

    # ------------------------------------------------------------------
    # 公开接口（与 SyntheticEquityFetcher 一致）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        启动历史数据回放。

        读取 CSV 文件，逐行解析并推入 data_queue。
        当文件读取完毕后自动停止。
        """
        self._running = True
        self._start_time = time.time()

        if not self.csv_path.exists():
            logger.error(f"CSV 文件不存在: {self.csv_path}")
            self._running = False
            return

        logger.info(
            f"历史数据回放器启动 | file={self.csv_path.name} | "
            f"speed={self.playback_speed}x"
        )

        try:
            await self._playback_loop()
        except asyncio.CancelledError:
            logger.info("回放协程被取消")
        except Exception:
            logger.exception("回放过程中发生异常")
        finally:
            self._running = False
            # 注入毒丸
            try:
                self.data_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

            elapsed = time.time() - self._start_time
            logger.info(
                f"历史数据回放结束 | 推送={self._messages_pushed} | "
                f"耗时={elapsed:.1f}s"
            )

    async def stop(self) -> None:
        """停止回放。"""
        self._running = False

    async def update_subscriptions(self, active_symbols: List[str]) -> None:
        """
        动态切换标的列表（回测模式下仅记录，不做实际订阅）。
        """
        self._current_symbols = set(active_symbols)
        self.stats["subscription_changes"] += 1

    # ------------------------------------------------------------------
    # 内部：回放主循环
    # ------------------------------------------------------------------

    async def _playback_loop(self) -> None:
        """
        读取 CSV 并逐行回放。

        速率控制逻辑：
          - playback_speed == 0：光速模式，不 sleep
          - playback_speed > 0：计算相邻行的 Δt，sleep(Δt / speed)
        """
        previous_ts: Optional[float] = None

        # 在线程池中读取文件（CSV 文件可能很大）
        rows = await asyncio.to_thread(self._read_csv)
        self.stats["csv_rows_total"] = len(rows)

        logger.info(f"CSV 加载完成 | rows={len(rows)}")

        for row in rows:
            if not self._running:
                break

            # 解析字段
            parsed = self._parse_row(row)
            if parsed is None:
                continue

            # 速率控制
            current_ts = parsed.get("local_timestamp", time.time())
            if self.playback_speed > 0 and previous_ts is not None:
                dt = current_ts - previous_ts
                if dt > 0:
                    wait_time = dt / self.playback_speed
                    # 限制单次等待不超过 10 秒（防止 CSV 中过大的时间间隔）
                    wait_time = min(wait_time, 10.0)
                    await asyncio.sleep(wait_time)

            previous_ts = current_ts

            # 推入队列
            try:
                self.data_queue.put_nowait(parsed)
                self._messages_pushed += 1
                self.stats["messages_received"] += 1
                self.stats["messages_parsed"] += 1
                symbol = parsed.get("symbol", "")
                if symbol:
                    self.stats["last_price"][symbol] = parsed["price"]
                    self.stats["last_update"][symbol] = current_ts
            except asyncio.QueueFull:
                self.stats["messages_dropped"] += 1

    # ------------------------------------------------------------------
    # 内部：CSV 解析
    # ------------------------------------------------------------------

    def _read_csv(self) -> List[Dict[str, str]]:
        """
        同步读取 CSV 文件，返回字典列表。

        Returns:
            [{"timestamp": "...", "symbol": "...", ...}, ...]
        """
        rows = []
        with open(self.csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def _parse_row(self, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        将 CSV 的一行解析为与实盘 WebSocket 完全相同的字典结构。

        字段映射（大小写不敏感）：
          timestamp / time  → local_timestamp
          symbol / ticker   → symbol
          price / last      → price
          volume_usdt       → volume_usdt
          last_trade_size   → last_trade_size
          volume / lastSz   → 用于推导 volume_usdt

        Returns:
            标准化行情 dict，解析失败返回 None
        """
        # 统一 key 为小写以便查找
        lower_row = {k.lower().strip(): v for k, v in row.items()}

        try:
            # ── 时间戳 ──
            ts_raw = lower_row.get("timestamp") or lower_row.get("time") or "0"
            try:
                # 尝试 UNIX 秒
                timestamp = float(ts_raw)
            except ValueError:
                # 尝试 ISO 8601 字符串
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    timestamp = dt.timestamp()
                except Exception:
                    timestamp = time.time()

            # ── 合约代码 ──
            symbol = (
                lower_row.get("symbol")
                or lower_row.get("ticker")
                or lower_row.get("instid")
                or ""
            )
            if not symbol:
                return None

            # ── 价格 ──
            price = float(
                lower_row.get("price")
                or lower_row.get("last")
                or lower_row.get("close")
                or 0
            )
            if price <= 0:
                return None

            # ── 成交量 / 成交额 ──
            volume_usdt = float(lower_row.get("volume_usdt", 0) or 0)
            last_trade_size = float(lower_row.get("last_trade_size", 0) or 0)

            # 若 volume_usdt 缺失，尝试从 volume 和 price 推导
            if volume_usdt <= 0:
                raw_vol = float(
                    lower_row.get("volume")
                    or lower_row.get("lastsz")
                    or lower_row.get("vol")
                    or 0
                )
                volume_usdt = raw_vol * price
                last_trade_size = raw_vol if last_trade_size <= 0 else last_trade_size

            # ── 可选字段 ──
            ask = float(lower_row.get("ask", 0) or 0)
            bid = float(lower_row.get("bid", 0) or 0)
            vol_quote_24h = float(lower_row.get("volume_quote_24h", 0) or 0)
            vol_base_24h = float(lower_row.get("volume_base_24h", 0) or 0)

            return {
                "symbol": symbol,
                "price": price,
                "last_trade_size": last_trade_size,
                "volume_usdt": volume_usdt,
                "volume_quote_24h": vol_quote_24h,
                "volume_base_24h": vol_base_24h,
                "ask": ask,
                "bid": bid,
                "spread": ask - bid if ask > 0 and bid > 0 else 0.0,
                "server_timestamp": timestamp,
                "local_timestamp": timestamp,
            }

        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"CSV 行解析失败: {e} | row={str(row)[:200]}")
            return None

    # ------------------------------------------------------------------
    # 公开：统计
    # ------------------------------------------------------------------

    @property
    def progress_pct(self) -> float:
        """回放进度百分比"""
        total = self.stats.get("csv_rows_total", 0)
        if total == 0:
            return 0.0
        return min(100.0, self._messages_pushed / total * 100.0)

    def __repr__(self) -> str:
        return (
            f"HistoricalCSVFeeder(file={self.csv_path.name}, "
            f"speed={self.playback_speed}x, "
            f"progress={self.progress_pct:.1f}%)"
        )
