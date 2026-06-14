"""
智能盘口控制器 (Session Manager)
=================================
通过 Alpaca TradingClient.get_clock() 实时监测美股交易时段。
当盘口关闭时，拦截下游的计算与下单请求，避免流动性枯竭时的伪信号。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from alpaca.trading.client import TradingClient

from config import alpaca_cfg

logger = logging.getLogger(__name__)


class SessionManager:
    """
    盘口状态管理器。
    - 后台轮询交易所时钟，维护当前是否开盘的布尔状态。
    - 通过 asyncio.Event 向外部组件广播状态变更。
    """

    def __init__(self, trading_client: TradingClient, poll_interval: float = 15.0):
        """
        Args:
            trading_client: Alpaca TradingClient 实例。
            poll_interval: 轮询间隔（秒）。建议 15~30 秒，避免触发频率限制。
        """
        self._client = trading_client
        self._poll_interval = poll_interval
        self._is_open: bool = False
        self._lock = asyncio.Lock()
        self._market_open_event = asyncio.Event()
        self._market_close_event = asyncio.Event()
        self._next_open: Optional[datetime] = None
        self._next_close: Optional[datetime] = None
        self._running = False

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """当前盘口是否处于可交易状态（盘中 + 盘前/盘后视 Alpaca 返回而定）。"""
        return self._is_open

    @property
    def next_open(self) -> Optional[datetime]:
        """下一次开盘时间（UTC）。"""
        return self._next_open

    @property
    def next_close(self) -> Optional[datetime]:
        """下一次收盘时间（UTC）。"""
        return self._next_close

    # ------------------------------------------------------------------
    # 核心轮询协程
    # ------------------------------------------------------------------
    async def _poll_clock(self) -> None:
        """
        后台任务：定期从 Alpaca REST API 拉取交易所时钟。
        当市场状态发生变化时，设置/清除对应的 asyncio.Event。
        """
        while self._running:
            try:
                clock = await asyncio.to_thread(self._client.get_clock)
            except Exception:
                logger.exception("拉取交易所时钟失败，将在下次轮询重试")
                await asyncio.sleep(self._poll_interval)
                continue

            async with self._lock:
                was_open = self._is_open
                self._is_open = clock.is_open
                self._next_open = clock.next_open
                self._next_close = clock.next_close

                if self._is_open and not was_open:
                    logger.info("🎯 盘口已开盘！下一次收盘: %s (UTC)", self._next_close)
                    self._market_open_event.set()
                    self._market_close_event.clear()
                elif not self._is_open and was_open:
                    logger.info("🔒 盘口已收盘！下一次开盘: %s (UTC)", self._next_open)
                    self._market_open_event.clear()
                    self._market_close_event.set()

            await asyncio.sleep(self._poll_interval)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动盘口轮询后台任务。启动时立即查询一次当前状态。"""
        self._running = True
        # 首次同步查询，确保状态在消费者启动前已就绪
        try:
            clock = await asyncio.to_thread(self._client.get_clock)
            async with self._lock:
                self._is_open = clock.is_open
                self._next_open = clock.next_open
                self._next_close = clock.next_close
                if self._is_open:
                    self._market_open_event.set()
                    self._market_close_event.clear()
                else:
                    self._market_open_event.clear()
                    self._market_close_event.set()
            logger.info(
                "盘口状态初始化完成: is_open=%s, next_open=%s, next_close=%s",
                self._is_open, self._next_open, self._next_close,
            )
        except Exception:
            logger.exception("初始化盘口状态失败，将在后台轮询中恢复")

        asyncio.create_task(self._poll_clock())

    async def stop(self) -> None:
        """停止后台轮询。"""
        self._running = False

    # ------------------------------------------------------------------
    # 门禁 —— 下游调用方通过此方法阻塞等待开盘状态
    # ------------------------------------------------------------------
    async def wait_until_open(self) -> None:
        """
        阻塞当前协程直到盘口开盘。
        若盘口已开盘则立即返回。
        这是系统能否下单的最终门禁，所有下单路径在发送订单前都应调用此方法。
        """
        if not self._is_open:
            logger.debug("盘口关闭中，等待开盘事件...")
            await self._market_open_event.wait()
