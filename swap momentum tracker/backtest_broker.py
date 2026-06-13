"""
虚拟撮合与清算所 (backtest_broker.py)
======================================
职责：
  1. 遵循 OMS 的 BaseExecutor 接口规范，为回测提供内存撮合引擎
  2. 内存记账：初始化虚拟资金池，按 Feeder 推送的最新价格撮合
  3. 摩擦成本：每次撮合扣除双边 0.05% 滑点与手续费
  4. 绩效评估：回测结束时输出净值/盈亏/胜率/最大回撤

依赖注入设计：
  实盘                     回测
  ─────                    ─────
  AlpacaPaperExecutor  ←  BacktestBroker (BaseExecutor)
  OKXLiveExecutor      ←  BacktestBroker (BaseExecutor)

  在 run_backtest.py 中，BacktestBroker 直接替换实盘执行器，
  OrderRouter.process_signal() 无需任何修改。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List

from order_manager import BaseExecutor

logger = logging.getLogger("SwapMomentum.BacktestBroker")


# ============================================================================
# 交易记录数据类
# ============================================================================

@dataclass
class TradeRecord:
    """单笔交易记录"""
    timestamp: float
    symbol: str
    action: str           # BUY / SELL
    qty: float            # 成交数量
    price: float          # 成交价格（含滑点）
    notional_value: float # 名义价值
    fee: float            # 手续费
    pnl: float = 0.0      # 平仓时的盈亏
    pnl_pct: float = 0.0  # 平仓时的盈亏百分比

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "action": self.action,
            "qty": self.qty,
            "price": self.price,
            "notional_value": self.notional_value,
            "fee": self.fee,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
        }


# ============================================================================
# 虚拟撮合引擎
# ============================================================================

class BacktestBroker(BaseExecutor):
    """
    回测虚拟撮合引擎。

    特性：
      - 内存记账：初始虚拟资金 100,000 USDT
      - 撮合价格 = 当前行情价 × (1 + slippage_sign × 0.025%)
      - 双边手续费 = notional_value × 0.05%
      - 支持多标的独立持仓追踪
      - generate_report() 输出完整绩效评估

    接口完全兼容 BaseExecutor，可直接注入 OrderRouter。
    """

    # 摩擦成本参数
    SLIPPAGE_BPS = 2.5        # 单边滑点（bps）
    COMMISSION_BPS = 2.5      # 单边手续费（bps）
    TOTAL_FRICTION_BPS = 5.0  # 双边合计（bps）

    def __init__(self, initial_capital: float = 100_000.0):
        """
        初始化虚拟撮合引擎。

        Args:
            initial_capital: 初始虚拟资金（USDT）
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital        # 可用现金
        self.equity = 0.0                  # 持仓市值

        # 多标的持仓：{ symbol: {"qty": 0, "avg_entry": 0, "notional": 0} }
        self.positions: Dict[str, Dict[str, float]] = {}

        # 交易历史
        self.trades: List[TradeRecord] = []

        # 净值曲线（用于计算最大回撤）
        self._equity_curve: List[tuple] = []  # [(timestamp, net_value), ...]

        # 统计
        self.stats = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
        }

        # 最后成交价缓存
        self._last_prices: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # BaseExecutor 接口实现
    # ------------------------------------------------------------------

    async def setup_account(self) -> None:
        """
        初始化回测账户。

        重置所有内部状态（支持多次回测复用）。
        """
        self.cash = self.initial_capital
        self.equity = 0.0
        self.positions.clear()
        self.trades.clear()
        self._equity_curve.clear()
        self._last_prices.clear()
        self.stats = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
        }
        logger.info(
            f"BacktestBroker 就绪 | 初始资金=${self.initial_capital:,.0f} | "
            f"摩擦成本={self.TOTAL_FRICTION_BPS}bps(双边)"
        )

    async def execute_order(
        self,
        symbol: str,
        action: str,
        notional_value: float,
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        执行虚拟撮合。

        流程：
          1. 计算滑点调整后的撮合价格
          2. 扣除双边手续费
          3. 更新持仓与现金
          4. 记录交易日志

        Args:
            symbol:         标的代码
            action:         BUY / SELL
            notional_value: 名义价值（USD）
            current_price:  当前行情参考价

        Returns:
            订单结果 dict，失败返回 None
        """
        self.stats["orders_submitted"] += 1

        if current_price <= 0:
            logger.error(f"BacktestBroker: {symbol} 无效价格 {current_price}")
            self.stats["orders_rejected"] += 1
            return None

        # 缓存最新价格
        self._last_prices[symbol] = current_price

        # ── 计算摩擦成本 ──
        # 买入使用 ask 方向滑点（价格微升），卖出使用 bid 方向（价格微跌）
        slippage_factor = 1.0 + (self.SLIPPAGE_BPS / 10000.0)
        if action.upper() == "BUY":
            fill_price = current_price * slippage_factor
        else:
            fill_price = current_price * (2.0 - slippage_factor)  # 1 - bps

        # 双边手续费
        fee = notional_value * (self.COMMISSION_BPS / 10000.0)

        # 数量
        qty = notional_value / fill_price

        logger.info(
            f"BacktestBroker 撮合 | {action} {symbol} | "
            f"qty={qty:.6f} @ ${fill_price:.4f} | "
            f"notional=${notional_value:,.0f} | fee=${fee:.2f}"
        )

        now = time.time()

        # ── BUY：开多 ──
        if action.upper() == "BUY":
            cost = notional_value + fee
            if self.cash < cost:
                logger.error(
                    f"BacktestBroker: 资金不足 | "
                    f"need=${cost:,.2f} | cash=${self.cash:,.2f}"
                )
                self.stats["orders_rejected"] += 1
                return None

            self.cash -= cost

            # 更新持仓
            if symbol in self.positions:
                pos = self.positions[symbol]
                old_qty = pos["qty"]
                old_notional = pos["notional"]
                new_qty = old_qty + qty
                new_notional = old_notional + notional_value
                pos["avg_entry"] = new_notional / new_qty if new_qty > 0 else 0
                pos["qty"] = new_qty
                pos["notional"] = new_notional
            else:
                self.positions[symbol] = {
                    "qty": qty,
                    "avg_entry": fill_price,
                    "notional": notional_value,
                }

            # 更新市值
            self._update_equity(now, current_price)

            trade = TradeRecord(
                timestamp=now,
                symbol=symbol,
                action="BUY",
                qty=qty,
                price=fill_price,
                notional_value=notional_value,
                fee=fee,
            )
            self.trades.append(trade)
            self.stats["orders_filled"] += 1

            return {
                "broker": "backtest",
                "order_id": f"bt_{len(self.trades)}",
                "symbol": symbol,
                "action": "BUY",
                "qty": qty,
                "filled_price": fill_price,
                "notional_value": notional_value,
                "fee": fee,
                "status": "filled",
            }

        # ── SELL：平多 ──
        if action.upper() == "SELL":
            if symbol not in self.positions or self.positions[symbol]["qty"] <= 0:
                logger.warning(f"BacktestBroker: {symbol} 无持仓，跳过平仓")
                self.stats["orders_rejected"] += 1
                return None

            pos = self.positions[symbol]
            close_qty = pos["qty"]
            close_notional = close_qty * fill_price
            proceeds = close_notional - fee

            # 计算盈亏
            avg_entry = pos["avg_entry"]
            pnl = (fill_price - avg_entry) * close_qty - fee * 2  # 开仓+平仓手续费
            pnl_pct = (pnl / (avg_entry * close_qty)) * 100.0 if avg_entry > 0 else 0.0

            self.cash += proceeds

            # 清除持仓
            del self.positions[symbol]

            # 更新市值
            self._update_equity(now, current_price)

            trade = TradeRecord(
                timestamp=now,
                symbol=symbol,
                action="SELL",
                qty=close_qty,
                price=fill_price,
                notional_value=close_notional,
                fee=fee,
                pnl=pnl,
                pnl_pct=pnl_pct,
            )
            self.trades.append(trade)
            self.stats["orders_filled"] += 1

            logger.info(
                f"BacktestBroker 平仓盈亏 | {symbol} | "
                f"PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)"
            )

            return {
                "broker": "backtest",
                "order_id": f"bt_{len(self.trades)}",
                "symbol": symbol,
                "action": "SELL",
                "qty": close_qty,
                "filled_price": fill_price,
                "notional_value": close_notional,
                "fee": fee,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "status": "filled",
            }

        return None

    # ------------------------------------------------------------------
    # 净值计算
    # ------------------------------------------------------------------

    def _update_equity(self, timestamp: float, current_price: float) -> None:
        """
        更新净值曲线。

        net_value = cash + Σ(持仓数量 × 当前价格)
        """
        position_value = 0.0
        for sym, pos in self.positions.items():
            price = self._last_prices.get(sym, current_price)
            position_value += pos["qty"] * price

        net_value = self.cash + position_value
        self._equity_curve.append((timestamp, net_value))

    def current_net_value(self) -> float:
        """当前净值"""
        if self._equity_curve:
            return self._equity_curve[-1][1]
        return self.initial_capital

    # ------------------------------------------------------------------
    # 绩效评估
    # ------------------------------------------------------------------

    def generate_report(self) -> Dict[str, Any]:
        """
        生成回测绩效报告。

        包含：
          - 初始/最终净值
          - 总盈亏（绝对 + 百分比）
          - 交易统计（次数、胜率）
          - 最大回撤 (Max Drawdown)
          - 夏普比率（简化版）
        """
        if not self._equity_curve:
            return {"error": "no_equity_data"}

        final_nav = self._equity_curve[-1][1]
        initial_nav = self.initial_capital
        total_pnl = final_nav - initial_nav
        total_pnl_pct = (total_pnl / initial_nav) * 100.0

        # ── 最大回撤 ──
        peak = initial_nav
        max_drawdown = 0.0
        max_drawdown_pct = 0.0
        for _, nav in self._equity_curve:
            if nav > peak:
                peak = nav
            dd = peak - nav
            dd_pct = (dd / peak) * 100.0 if peak > 0 else 0.0
            if dd_pct > max_drawdown_pct:
                max_drawdown = dd
                max_drawdown_pct = dd_pct

        # ── 交易统计 ──
        sell_trades = [t for t in self.trades if t.action == "SELL"]
        buy_trades = [t for t in self.trades if t.action == "BUY"]
        winning_trades = [t for t in sell_trades if t.pnl > 0]
        losing_trades = [t for t in sell_trades if t.pnl <= 0]

        total_trades = len(sell_trades)
        win_count = len(winning_trades)
        win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0

        avg_win = (
            sum(t.pnl for t in winning_trades) / win_count
            if win_count > 0 else 0.0
        )
        avg_loss = (
            sum(t.pnl for t in losing_trades) / len(losing_trades)
            if losing_trades else 0.0
        )

        # 盈亏比
        profit_factor = (
            abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        )

        # ── 简化夏普比率（假设无风险利率=0） ──
        returns = []
        for i in range(1, len(self._equity_curve)):
            r = (self._equity_curve[i][1] - self._equity_curve[i-1][1]) / self._equity_curve[i-1][1]
            returns.append(r)
        if returns:
            mean_ret = sum(returns) / len(returns)
            std_ret = (
                sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            ) ** 0.5
            sharpe = (mean_ret / std_ret) if std_ret > 0 else 0.0
            # 年化（假设每个 tick 约 0.1s，年化因子 √(365×24×3600×10)）
            ann_sharpe = sharpe * (365 * 24 * 3600 * 10) ** 0.5
        else:
            sharpe = 0.0
            ann_sharpe = 0.0

        # ── 总摩擦成本 ──
        total_fees = sum(t.fee for t in self.trades)
        total_slippage = total_pnl - (
            sum(t.pnl for t in sell_trades) - total_fees
        )

        return {
            "initial_capital": initial_nav,
            "final_nav": round(final_nav, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "total_trades": total_trades,
            "buy_count": len(buy_trades),
            "sell_count": total_trades,
            "win_count": win_count,
            "lose_count": len(losing_trades),
            "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
            "sharpe_ratio": round(sharpe, 4),
            "ann_sharpe": round(ann_sharpe, 4),
            "total_fees": round(total_fees, 2),
            "total_slippage_impact": round(total_slippage, 2),
            "equity_curve": self._equity_curve,
            "trades": [t.to_dict() for t in self.trades],
        }

    def format_report(self) -> str:
        """格式化绩效报告为文本。"""
        r = self.generate_report()
        if "error" in r:
            return "无有效回测数据"

        return f"""
{'=' * 64}
  回测绩效报告
{'=' * 64}

  初始资金    : ${r['initial_capital']:>12,.2f}
  最终净值    : ${r['final_nav']:>12,.2f}
  总盈亏      : ${r['total_pnl']:>+12,.2f}  ({r['total_pnl_pct']:+.2f}%)

  ── 风险指标 ──
  最大回撤    : ${r['max_drawdown']:>12,.2f}  ({r['max_drawdown_pct']:.2f}%)
  夏普比率    : {r['sharpe_ratio']:>12.4f}  (年化: {r['ann_sharpe']:.2f})

  ── 交易统计 ──
  总交易次数  : {r['total_trades']:>12}
  盈利次数    : {r['win_count']:>12}
  胜率        : {r['win_rate']:>11.2f}%
  平均盈利    : ${r['avg_win']:>+12,.2f}
  平均亏损    : ${r['avg_loss']:>+12,.2f}
  盈亏比      : {r['profit_factor']:>12.2f}

  ── 成本分析 ──
  总手续费    : ${r['total_fees']:>12,.2f}
  滑点影响    : ${r['total_slippage_impact']:>12,.2f}

{'=' * 64}
"""
