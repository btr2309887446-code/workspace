"""
订单与仓位管理系统 (OMS/PMS)
============================
基于 Alpaca TradingClient 实现：
- 账户资金与持仓同步
- 动态仓位计算（单次开仓 ≤ 总购买力 10%）
- 做多专用（无持仓时拒绝 SELL）
- 统一下达市价单以保证动能突破时的绝对成交
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Position
from alpaca.trading.requests import MarketOrderRequest

from config import strategy_cfg, alpaca_cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class AccountSnapshot:
    """账户资金快照。"""
    buying_power: float     # 可用购买力（含保证金）
    cash: float             # 现金余额
    equity: float           # 总权益
    positions_count: int    # 当前持仓数量


@dataclass
class OrderResult:
    """下单结果。"""
    symbol: str
    side: str               # "BUY" | "SELL"
    qty: int
    order_id: str
    filled_avg_price: Optional[float]
    status: str


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------
class OrderManager:
    """
    订单执行与仓位管理者。
    策略：仅做多。
    - BUY → 计算股数 → 下市价单
    - SELL → 若持有多头 → close_position（全平）；若无持仓 → 拒绝
    """

    def __init__(self, trading_client: TradingClient):
        self._client = trading_client
        self._max_ratio = strategy_cfg.MAX_POSITION_RATIO

    # ------------------------------------------------------------------
    # 资金与持仓查询
    # ------------------------------------------------------------------
    async def get_account_snapshot(self) -> AccountSnapshot:
        """
        异步获取当前账户资金快照。
        使用 asyncio.to_thread 将 Alpaca SDK 的同步调用迁移到线程池，
        避免阻塞事件循环。
        """
        try:
            account = await asyncio.to_thread(self._client.get_account)
            positions = await asyncio.to_thread(self._client.get_all_positions)
            return AccountSnapshot(
                buying_power=float(account.buying_power),
                cash=float(account.cash),
                equity=float(account.equity),
                positions_count=len(positions),
            )
        except Exception:
            logger.exception("获取账户快照失败")
            raise

    async def get_position(self, symbol: str) -> Optional[Position]:
        """获取指定标的的当前持仓，若无则返回 None。"""
        try:
            pos = await asyncio.to_thread(self._client.get_open_position, symbol)
            return pos
        except Exception:
            # Alpaca SDK 在无持仓时抛出异常（通常是 APIError 404）
            return None

    async def get_all_positions(self) -> List[Position]:
        """获取所有当前持仓。"""
        try:
            return await asyncio.to_thread(self._client.get_all_positions)
        except Exception:
            logger.exception("获取所有持仓失败")
            return []

    # ------------------------------------------------------------------
    # 仓位计算
    # ------------------------------------------------------------------
    async def _calc_buy_qty(self, symbol: str, price: float) -> int:
        """
        基于可用购买力与仓位上限，计算应买入的股数。
        公式: qty = floor(buying_power × 10% / price)
        最小 1 股，取整。

        Args:
            symbol: 股票代码。
            price: 参考价格（用于估算股数）。

        Returns:
            应买入的整数股数（最小 1 股）。
        """
        try:
            snapshot = await self.get_account_snapshot()
            max_amount = snapshot.buying_power * self._max_ratio
            qty = int(max_amount / price)
            return max(qty, 1)
        except Exception:
            logger.exception("[%s] 计算买入股数失败", symbol)
            return 1  # 兜底：至少 1 股

    # ------------------------------------------------------------------
    # 买入执行
    # ------------------------------------------------------------------
    async def execute_buy(self, symbol: str, price: float,
                          confidence: float) -> Optional[OrderResult]:
        """
        执行买入操作。

        Args:
            symbol: 股票代码。
            price: 触发信号的当前价格（用于估算股数）。
            confidence: LLM 置信度（0~1）。

        Returns:
            OrderResult 或 None（失败时）。
        """
        try:
            # 检查是否已持有该标的多头
            existing = await self.get_position(symbol)
            if existing is not None:
                logger.info(
                    "[%s] 已持有多头 (%s 股，均价 %s)，跳过重复买入",
                    symbol, existing.qty, existing.avg_entry_price,
                )
                return None

            # 计算股数
            qty = await self._calc_buy_qty(symbol, price)

            # 根据置信度微调仓位：高置信度时可适当增加，但不超过上限
            qty = int(qty * max(confidence, 0.5))

            # 构建市价买单
            order_req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )

            # 下单（同步调用 -> 线程池）
            order = await asyncio.to_thread(self._client.submit_order, order_req)

            result = OrderResult(
                symbol=symbol,
                side="BUY",
                qty=qty,
                order_id=str(order.id),
                filled_avg_price=(
                    float(order.filled_avg_price) if order.filled_avg_price else None
                ),
                status=str(order.status),
            )
            logger.info(
                "[%s] 📈 已提交买单！股数=%d | 订单ID=%s | 状态=%s",
                symbol, qty, result.order_id, result.status,
            )
            return result

        except Exception:
            logger.exception("[%s] 执行买入失败", symbol)
            return None

    # ------------------------------------------------------------------
    # 卖出执行（平仓）
    # ------------------------------------------------------------------
    async def execute_sell(self, symbol: str) -> Optional[OrderResult]:
        """
        执行卖出（平仓）操作。
        使用 close_position 平掉全部多头仓位。若无持仓则拒绝。

        Args:
            symbol: 股票代码。

        Returns:
            OrderResult 或 None。
        """
        try:
            existing = await self.get_position(symbol)
            if existing is None:
                logger.info("[%s] 无持仓，拒绝 SELL 信号", symbol)
                return None

            # 全平持仓
            closed = await asyncio.to_thread(self._client.close_position, symbol)

            result = OrderResult(
                symbol=symbol,
                side="SELL",
                qty=int(existing.qty),
                order_id=str(closed.id),
                filled_avg_price=(
                    float(closed.filled_avg_price)
                    if closed.filled_avg_price
                    else None
                ),
                status=str(closed.status),
            )
            logger.info(
                "[%s] 📉 已平仓！股数=%s | 订单ID=%s | 状态=%s",
                symbol, existing.qty, result.order_id, result.status,
            )
            return result

        except Exception:
            logger.exception("[%s] 执行卖出失败", symbol)
            return None
