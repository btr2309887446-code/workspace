"""
动能微积分引擎 (Analytics)
==========================
核心算法模块：通过价格波动的一阶导数（速度 Velocity）与成交量（Volume）
的乘积得出真实资金动能（能量 Energy），在能量突破阈值时触发异动告警。

数学定义：
  P(t)  = 最新成交价格
  ΔP    = P(t) - P(t-1)
  V(t)  = EMA(ΔP, α)          # 速度 = 价格一阶导数的 EMA 平滑
  E(t)  = |V(t)| × Volume(t)  # 能量 = |速度| × 成交量
  I(t)  = ∫ E(t) dt           # 5分钟窗口能量积分

数据结构：
  使用 collections.deque 作为环形缓冲区，maxlen 防止内存泄漏。
  每条 tick 记录: (timestamp, price, volume, velocity, energy)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional, Tuple

from config import strategy_cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 单条 tick 记录
# ---------------------------------------------------------------------------
@dataclass
class TickRecord:
    """单条行情快照的计算结果。"""
    timestamp: float          # Unix epoch 秒（monotonic 或 UTC）
    price: float
    volume: int
    velocity: float           # EMA 平滑后的速度
    energy: float             # |velocity| × volume


# ---------------------------------------------------------------------------
# 异动信号
# ---------------------------------------------------------------------------
@dataclass
class MomentumSignal:
    """当能量突破阈值时发出的异动信号。"""
    symbol: str
    timestamp: float
    price: float
    velocity: float
    energy: float
    window_energy_integral: float   # 5分钟能量积分
    avg_velocity: float             # 5分钟平均速度


# ---------------------------------------------------------------------------
# MarketDynamicsCalculator
# ---------------------------------------------------------------------------
class MarketDynamicsCalculator:
    """
    单标的市场微观结构动力学计算器。
    每个标的（如 TSLA、NVDA）各自维护一个实例。
    """

    def __init__(self, symbol: str):
        """
        Args:
            symbol: 股票代码，如 "TSLA"。
        """
        self.symbol = symbol
        self._alpha = strategy_cfg.EMA_ALPHA
        self._threshold = strategy_cfg.ENERGY_THRESHOLD
        self._window_sec = strategy_cfg.WINDOW_SECONDS
        self._maxlen = strategy_cfg.DEQUE_MAXLEN

        # 环形缓冲区：存储 TickRecord
        self._buffer: Deque[TickRecord] = deque(maxlen=self._maxlen)

        # EMA 内部状态
        self._prev_price: Optional[float] = None
        self._prev_velocity: Optional[float] = None

        # 用于触发下游的信号队列
        self._signal_queue: asyncio.Queue = asyncio.Queue()

    # ------------------------------------------------------------------
    # 核心计算
    # ------------------------------------------------------------------
    def update(self, price: float, volume: int, timestamp: Optional[float] = None) -> Optional[MomentumSignal]:
        """
        输入新到来的价格与成交量，更新内部状态。
        若瞬时能量超过阈值，返回 MomentumSignal；否则返回 None。

        Args:
            price: 最新成交价。
            volume: 本次成交量（股）。
            timestamp: Unix 时间戳。若为 None，使用当前时间。

        Returns:
            MomentumSignal 或 None。
        """
        ts = timestamp if timestamp is not None else time.time()

        # ---- 速度计算 (dP/dt 的 EMA 平滑) ----
        if self._prev_price is None:
            # 第一笔 tick：初始化状态，不产生信号
            self._prev_price = price
            self._prev_velocity = 0.0
            return None

        raw_velocity = price - self._prev_price  # ΔP
        self._prev_price = price

        # EMA: V_t = α × ΔP + (1 - α) × V_{t-1}
        velocity = (
            self._alpha * raw_velocity
            + (1 - self._alpha) * (self._prev_velocity or 0.0)
        )
        self._prev_velocity = velocity

        # ---- 能量计算 ----
        energy = abs(velocity) * abs(volume)

        # ---- 写入环形缓冲区 ----
        record = TickRecord(
            timestamp=ts,
            price=price,
            volume=volume,
            velocity=velocity,
            energy=energy,
        )
        self._buffer.append(record)

        # ---- 阈值检测 ----
        if energy < self._threshold:
            return None

        window_energy, avg_vel = self._compute_window_stats()
        signal = MomentumSignal(
            symbol=self.symbol,
            timestamp=ts,
            price=price,
            velocity=velocity,
            energy=energy,
            window_energy_integral=window_energy,
            avg_velocity=avg_vel,
        )
        # 非阻塞推入异步队列
        try:
            self._signal_queue.put_nowait(signal)
        except asyncio.QueueFull:
            logger.warning("[%s] 信号队列已满，丢弃信号", self.symbol)

        logger.info(
            "[%s] 🚨 动能异动！Energy=%.2f | Velocity=%.4f | Price=%.2f | Vol=%d",
            self.symbol, energy, velocity, price, volume,
        )
        return signal

    # ------------------------------------------------------------------
    # 窗口统计
    # ------------------------------------------------------------------
    def _compute_window_stats(self) -> Tuple[float, float]:
        """
        计算 5 分钟窗口内的能量积分与平均速度。

        Returns:
            (window_energy_integral, avg_velocity)
        """
        now = time.time()
        cutoff = now - self._window_sec

        window_energy = 0.0
        velocity_sum = 0.0
        count = 0

        # 逆序遍历环形缓冲区（最近 -> 最旧）
        for record in reversed(self._buffer):
            if record.timestamp < cutoff:
                break
            window_energy += record.energy
            velocity_sum += record.velocity
            count += 1

        avg_velocity = velocity_sum / count if count > 0 else 0.0
        return window_energy, avg_velocity

    def get_5min_stats(self) -> Dict[str, float]:
        """
        获取当前窗口的统计信息（外部查询接口）。

        Returns:
            dict with keys: energy_integral, avg_velocity, tick_count
        """
        now = time.time()
        cutoff = now - self._window_sec
        energy_sum = 0.0
        vel_sum = 0.0
        count = 0
        for r in reversed(self._buffer):
            if r.timestamp < cutoff:
                break
            energy_sum += r.energy
            vel_sum += r.velocity
            count += 1
        return {
            "energy_integral": energy_sum,
            "avg_velocity": vel_sum / count if count else 0.0,
            "tick_count": count,
        }

    # ------------------------------------------------------------------
    # 信号消费接口
    # ------------------------------------------------------------------
    @property
    def signal_queue(self) -> asyncio.Queue:
        """下游通过此队列异步消费异动信号。"""
        return self._signal_queue


# ---------------------------------------------------------------------------
# 多标的切片管理器
# ---------------------------------------------------------------------------
class MultiSymbolAnalytics:
    """
    管理多个标的的 MarketDynamicsCalculator 实例。
    消费行情队列，按 symbol 分发给对应的计算器。
    """

    def __init__(self, tickers: list):
        self._calculators: Dict[str, MarketDynamicsCalculator] = {
            t: MarketDynamicsCalculator(t) for t in tickers
        }

    def get_calculator(self, symbol: str) -> Optional[MarketDynamicsCalculator]:
        return self._calculators.get(symbol)

    def feed(self, symbol: str, price: float, volume: int, timestamp: Optional[float] = None) -> Optional[MomentumSignal]:
        """向指定标的喂入行情数据。"""
        calc = self._calculators.get(symbol)
        if calc is None:
            return None
        return calc.update(price, volume, timestamp)

    @property
    def all_calculators(self) -> Dict[str, MarketDynamicsCalculator]:
        return self._calculators
