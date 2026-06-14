"""
极速行情源 (Data Fetcher)
==========================
基于 alpaca-py 的 StockDataStream (WebSocket) 订阅实时行情，
将逐笔成交 (Trade) 和最优报价 (Quote) 清洗为统一字典后推入 asyncio.Queue，
供下游 analytics 模块消费。

特性：
- 自动重连（WebSocket 断开后自动恢复）
- 异常隔离（单个标的的数据异常不影响其他标的）
- 非阻塞推送
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from alpaca.data.live import StockDataStream
from alpaca.data.models import Trade, Quote

from config import strategy_cfg, alpaca_cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 行情数据格式化
# ---------------------------------------------------------------------------
def _format_trade(trade: Trade) -> Dict[str, Any]:
    """将 Alpaca Trade 对象清洗为标准字典。"""
    return {
        "type": "trade",
        "symbol": str(trade.symbol),
        "price": float(trade.price),
        "size": int(trade.size),
        "volume": int(trade.size),  # size 即为本次成交的股数，用于能量计算
        "timestamp": (
            trade.timestamp.replace(tzinfo=timezone.utc)
            if trade.timestamp.tzinfo is None
            else trade.timestamp
        ),
        "conditions": getattr(trade, "conditions", None),
        "exchange": getattr(trade, "exchange", None),
    }


def _format_quote(quote: Quote) -> Dict[str, Any]:
    """将 Alpaca Quote 对象清洗为标准字典。"""
    mid_price = (float(quote.bid_price) + float(quote.ask_price)) / 2.0
    return {
        "type": "quote",
        "symbol": str(quote.symbol),
        "bid_price": float(quote.bid_price),
        "ask_price": float(quote.ask_price),
        "mid_price": mid_price,
        "bid_size": int(quote.bid_size),
        "ask_size": int(quote.ask_size),
        "timestamp": (
            quote.timestamp.replace(tzinfo=timezone.utc)
            if quote.timestamp.tzinfo is None
            else quote.timestamp
        ),
        "conditions": getattr(quote, "conditions", None),
    }


# ---------------------------------------------------------------------------
# DataFetcher
# ---------------------------------------------------------------------------
class DataFetcher:
    """
    实时行情数据获取器。
    通过 WebSocket 订阅目标标的的 Trade 和 Quote 频道，
    将标准化后的数据非阻塞推入 asyncio.Queue。
    """

    def __init__(self, queue: asyncio.Queue):
        """
        Args:
            queue: 下游消费队列。所有行情数据将推送至此队列。
        """
        self._queue = queue
        self._tickers = strategy_cfg.TICKERS
        self._stream: Optional[StockDataStream] = None
        self._running = False
        self._reconnect_delay = 3.0  # 断线重连等待秒数

    # ------------------------------------------------------------------
    # 行情回调（由 StockDataStream 在收到数据时触发）
    # ------------------------------------------------------------------
    async def _on_trade(self, trade: Trade) -> None:
        """逐笔成交回调：清洗后入队。"""
        try:
            data = _format_trade(trade)
            await self._queue.put(data)
        except Exception:
            logger.exception("格式化 Trade 数据异常")

    async def _on_quote(self, quote: Quote) -> None:
        """报价回调：计算中间价后入队。"""
        try:
            data = _format_quote(quote)
            await self._queue.put(data)
        except Exception:
            logger.exception("格式化 Quote 数据异常")

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def _build_stream(self) -> StockDataStream:
        """构造 StockDataStream 实例并订阅所有目标标的。"""
        stream = StockDataStream(
            api_key=alpaca_cfg.API_KEY,
            secret_key=alpaca_cfg.API_SECRET,
            feed=alpaca_cfg.DATA_FEED,
        )
        # 逐标的订阅 Trade 和 Quote 频道
        for ticker in self._tickers:
            stream.subscribe_trades(self._on_trade, ticker)
            stream.subscribe_quotes(self._on_quote, ticker)
            logger.info("已订阅 %s 的 Trade + Quote 频道", ticker)
        return stream

    # ------------------------------------------------------------------
    # 主运行协程（带自动重连）
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """
        启动 WebSocket 行情流，并具备无限自动重连能力。
        任何网络抖动或服务端断连都会被捕获并在延迟后重试。
        """
        self._running = True
        logger.info("行情数据源启动，目标标的: %s", self._tickers)

        while self._running:
            try:
                self._stream = self._build_stream()
                logger.info("正在连接 Alpaca WebSocket 行情流...")
                # StockDataStream.run() 本身就是个无限循环的 async 方法
                await self._stream.run()
            except asyncio.CancelledError:
                logger.info("行情数据源收到取消信号，退出主循环")
                break
            except Exception:
                if not self._running:
                    break
                logger.exception(
                    "WebSocket 行情流断开，%s 秒后自动重连...", self._reconnect_delay
                )
                await asyncio.sleep(self._reconnect_delay)

    # ------------------------------------------------------------------
    # 优雅关闭
    # ------------------------------------------------------------------
    async def stop(self) -> None:
        """
        关闭 WebSocket 连接。
        StockDataStream 内部使用 websockets 库，调用 stop() 会发送关闭帧。
        """
        self._running = False
        if self._stream is not None:
            try:
                await self._stream.stop()
                logger.info("WebSocket 行情流已关闭")
            except Exception:
                logger.exception("关闭 WebSocket 时发生异常（可忽略）")
