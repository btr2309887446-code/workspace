"""
量化核心算法模块 (analytics.py)
================================
职责：
  1. 实时接收股票行情数据流，计算每只标的的价格"速度（Velocity）"与"能量（Energy）"
  2. 处理股票特有的开盘跳空缺口——跨长时间间隔的 Tick 不计算速度，防止污染 EMA
  3. 维护完整历史 Tick 记录（环形缓冲区），支持按时间窗口查询聚合统计
  4. 为 reporter.py 和 llm_agent.py 提供量化特征数据
  5. 零内存泄漏——所有缓冲区由 collections.deque 自动淘汰旧数据

算法说明：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Velocity (速度)：
    raw_velocity  = (P_t - P_{t-1}) / Δt              —— 瞬时价格变化率
    ema_velocity  = α × raw_velocity + (1-α) × ema_prev —— EMA 平滑
    ★ 跳空处理：若 Δt > gap_threshold_seconds（如 300s），跳过该 Tick 的速度计算

  Energy (能量)：
    energy = |ema_velocity| × volume（成交量加权动能）
    物理含义：推动价格变动的"资金强度"

  能量积分 (∫E dt)：
    梯形法则近似：Σ (E_i + E_{i-1}) / 2 × Δt_i
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from collections import deque
from typing import Dict, Any, Optional, List


class MarketDynamicsCalculator:
    """
    市场动力学指标计算器（单标的版本）。

    每个股票实例化一个独立计算器，由 pipeline.py 统一管理：
      calculators = {"AAPL": MarketDynamicsCalculator(s), ...}

    特性：
      - 跳空缺口智能检测（dt > gap_threshold 时跳过）
      - EMA 平滑的速度与成交量加权能量
      - get_window_stats(since_ts) 供 5 分钟报告使用
    """

    def __init__(self, settings):
        """
        初始化计算器。

        Args:
            settings: Settings 配置实例
        """
        self.settings = settings
        self.alpha: float = settings.velocity_ema_alpha
        self.window: int = settings.energy_window
        self.maxlen: int = settings.history_maxlen
        self.gap_threshold: float = settings.gap_threshold_seconds

        # 完整历史环形缓冲区
        # 元素: {ts, price, volume, volume_usdt, velocity, energy, raw_speed}
        self._tick_history: deque = deque(maxlen=self.maxlen)

        # 实时看板小窗口
        self._velocity_history: deque = deque(maxlen=self.window)
        self._energy_history: deque = deque(maxlen=self.window)

        # EMA 状态
        self._ema_velocity: float = 0.0
        self._initialized: bool = False

        # 上一采样点
        self._last_price: Optional[float] = None
        self._last_timestamp: Optional[float] = None

        # 最新值缓存
        self._current_price: float = 0.0
        self._current_velocity: float = 0.0
        self._current_energy: float = 0.0
        self._current_raw_speed: float = 0.0
        self._current_timestamp: float = 0.0
        self._update_count: int = 0
        self._gap_skip_count: int = 0

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def price(self) -> float:
        return self._current_price

    @property
    def velocity(self) -> float:
        return self._current_velocity

    @property
    def energy(self) -> float:
        return self._current_energy

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def sample_count(self) -> int:
        return self._update_count

    @property
    def tick_count_in_buffer(self) -> int:
        return len(self._tick_history)

    @property
    def gap_skip_count(self) -> int:
        return self._gap_skip_count

    # ------------------------------------------------------------------
    # 核心数据更新
    # ------------------------------------------------------------------

    def update(
        self, price: float, volume: float, timestamp: float
    ) -> Dict[str, Any]:
        """
        喂入一个新的采样点，更新所有指标。

        Args:
            price:     最新成交价
            volume:    成交量（股数）
            timestamp: UNIX 时间戳（秒）

        Returns:
            当前指标快照
        """
        self._update_count += 1
        self._current_price = price
        self._current_timestamp = timestamp

        volume_usdt = volume * price  # 成交额的美元价值

        if self._last_price is not None and self._last_timestamp is not None:
            dt = timestamp - self._last_timestamp
            dp = price - self._last_price

            # ── 跳空缺口检测 ──
            if dt > self.gap_threshold:
                # 跨长时间间隔（开盘跳空、盘中停牌恢复等），跳过速度计算
                # 但仍更新"上一采样点"，确保下次 Tick 能正常计算
                self._gap_skip_count += 1
                logger_skip = getattr(self, '_logged_gap', False)
                if not logger_skip:
                    logger_skip = True
                self._current_velocity = 0.0
                self._current_energy = 0.0
                self._current_raw_speed = 0.0
            elif dt > 0:
                raw_velocity = dp / dt
                self._current_raw_speed = raw_velocity

                if not self._initialized:
                    self._ema_velocity = raw_velocity
                    self._initialized = True
                else:
                    self._ema_velocity = (
                        self.alpha * raw_velocity
                        + (1.0 - self.alpha) * self._ema_velocity
                    )

                self._current_velocity = self._ema_velocity
                self._velocity_history.append((timestamp, self._ema_velocity))

                energy = abs(self._ema_velocity) * volume
                self._current_energy = energy
                self._energy_history.append((timestamp, energy))
        else:
            self._current_velocity = 0.0
            self._current_energy = 0.0
            self._current_raw_speed = 0.0

        # 追加到完整历史缓冲区
        self._tick_history.append({
            "ts": timestamp,
            "price": price,
            "volume": volume,
            "volume_usdt": volume_usdt,
            "velocity": self._current_velocity,
            "energy": self._current_energy,
            "raw_speed": self._current_raw_speed,
        })

        self._last_price = price
        self._last_timestamp = timestamp

        return {
            "price": self._current_price,
            "velocity": self._current_velocity,
            "energy": self._current_energy,
            "raw_speed": self._current_raw_speed,
            "timestamp": self._current_timestamp,
            "initialized": self._initialized,
            "gap_skip_count": self._gap_skip_count,
        }

    # ------------------------------------------------------------------
    # 窗口查询（供 reporter.py 和 llm_agent.py 使用）
    # ------------------------------------------------------------------

    def get_window_stats(self, since_ts: float) -> Optional[Dict[str, Any]]:
        """
        获取自指定时间戳以来的聚合统计数据。

        Args:
            since_ts: 窗口起始时间戳（UNIX 秒）

        Returns:
            None 或聚合统计 dict
        """
        window = [t for t in self._tick_history if t["ts"] >= since_ts]
        if len(window) < 2:
            return None

        velocities = [t["velocity"] for t in window]
        energies = [t["energy"] for t in window]
        times = [t["ts"] for t in window]
        prices = [t["price"] for t in window]
        volumes = [t["volume"] for t in window]
        n = len(window)

        # 平均速度
        avg_velocity = sum(velocities) / n

        # 速度标准差
        variance = sum((v - avg_velocity) ** 2 for v in velocities) / n
        std_velocity = variance ** 0.5

        # 能量积分（梯形法则）
        energy_integral = 0.0
        for i in range(1, n):
            dt_i = times[i] - times[i - 1]
            if dt_i > 0:
                energy_integral += (energies[i] + energies[i - 1]) / 2.0 * dt_i

        # 平均能量
        avg_energy = sum(energies) / n

        # 极值
        max_energy = max(energies)
        max_idx = energies.index(max_energy)
        min_energy = min(energies)
        min_idx = energies.index(min_energy)

        max_velocity = max(velocities)
        max_vel_idx = velocities.index(max_velocity)
        min_velocity = min(velocities)
        min_vel_idx = velocities.index(min_velocity)

        # 价格变动
        first_price = window[0]["price"]
        last_price = window[-1]["price"]
        price_change_pct = (
            (last_price - first_price) / first_price * 100.0
            if first_price > 0 else 0.0
        )

        # 方向判定
        net_velocity = sum(velocities)
        if net_velocity > 0.001:
            direction = "买盘主导 (偏多头)"
        elif net_velocity < -0.001:
            direction = "卖盘主导 (偏空头)"
        else:
            direction = "多空均衡 (震荡)"

        # 买卖力量比
        pos_energy = sum(
            energies[i] for i in range(n) if velocities[i] > 0
        )
        neg_energy = sum(
            energies[i] for i in range(n) if velocities[i] < 0
        )
        total_energy = pos_energy + neg_energy
        bull_ratio = (
            (pos_energy / total_energy * 100.0)
            if total_energy > 0 else 50.0
        )

        # 成交量统计
        total_volume = sum(volumes)
        avg_price = sum(prices) / n

        return {
            "start_time": times[0],
            "end_time": times[-1],
            "sample_count": n,
            "first_price": first_price,
            "last_price": last_price,
            "avg_price": avg_price,
            "price_change_pct": price_change_pct,
            "total_volume": total_volume,
            "avg_velocity": avg_velocity,
            "std_velocity": std_velocity,
            "max_velocity": max_velocity,
            "max_velocity_ts": times[max_vel_idx],
            "min_velocity": min_velocity,
            "min_velocity_ts": times[min_vel_idx],
            "energy_integral": energy_integral,
            "avg_energy": avg_energy,
            "max_energy": max_energy,
            "max_energy_ts": times[max_idx],
            "max_energy_price": window[max_idx]["price"],
            "max_energy_velocity": window[max_idx]["velocity"],
            "min_energy": min_energy,
            "min_energy_ts": times[min_idx],
            "min_energy_price": window[min_idx]["price"],
            "min_energy_velocity": window[min_idx]["velocity"],
            "direction": direction,
            "net_velocity": net_velocity,
            "bull_ratio": bull_ratio,
        }

    def get_recent_snapshot(self, n: int = 10) -> Dict[str, Any]:
        """
        获取最近的指标快照（供 LLM 分析使用）。

        Returns:
            {
                "latest": {price, velocity, energy, ...},
                "recent_velocities": [(ts, vel), ...],
                "recent_energies": [(ts, ene), ...],
                "velocity_mean": float,
                "energy_mean": float,
                "trend": "up"/"down"/"flat",
            }
        """
        ticks = list(self._tick_history)[-n:] if self._tick_history else []

        if not ticks:
            return {"error": "no_data"}

        velocities = [t["velocity"] for t in ticks if t["velocity"] != 0]
        energies = [t["energy"] for t in ticks]

        v_mean = sum(velocities) / len(velocities) if velocities else 0.0
        e_mean = sum(energies) / len(energies) if energies else 0.0

        if v_mean > 0.001:
            trend = "up"
        elif v_mean < -0.001:
            trend = "down"
        else:
            trend = "flat"

        return {
            "latest": ticks[-1],
            "recent_velocities": [(t["ts"], t["velocity"]) for t in ticks[-5:]],
            "recent_energies": [(t["ts"], t["energy"]) for t in ticks[-5:]],
            "velocity_mean": v_mean,
            "energy_mean": e_mean,
            "trend": trend,
            "sample_count": len(ticks),
        }

    # ------------------------------------------------------------------
    # 实时看板统计
    # ------------------------------------------------------------------

    def get_velocity_stats(self) -> Dict[str, float]:
        """速度序列统计摘要。"""
        if len(self._velocity_history) == 0:
            return {"mean": 0, "std": 0, "min": 0, "max": 0, "latest": 0}

        velocities = [v for _, v in self._velocity_history]
        n = len(velocities)
        mean = sum(velocities) / n
        variance = sum((v - mean) ** 2 for v in velocities) / n

        return {
            "mean": round(mean, 8),
            "std": round(variance ** 0.5, 8),
            "min": round(min(velocities), 8),
            "max": round(max(velocities), 8),
            "latest": round(self._current_velocity, 8),
        }

    def get_energy_stats(self) -> Dict[str, float]:
        """能量序列统计摘要。"""
        if len(self._energy_history) == 0:
            return {"mean": 0, "std": 0, "min": 0, "max": 0, "latest": 0}

        energies = [e for _, e in self._energy_history]
        n = len(energies)
        mean = sum(energies) / n
        variance = sum((e - mean) ** 2 for e in energies) / n

        return {
            "mean": round(mean, 8),
            "std": round(variance ** 0.5, 8),
            "min": round(min(energies), 8),
            "max": round(max(energies), 8),
            "latest": round(self._current_energy, 8),
        }

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置所有内部状态。"""
        self._tick_history.clear()
        self._velocity_history.clear()
        self._energy_history.clear()
        self._ema_velocity = 0.0
        self._initialized = False
        self._last_price = None
        self._last_timestamp = None
        self._current_price = 0.0
        self._current_velocity = 0.0
        self._current_energy = 0.0
        self._current_raw_speed = 0.0
        self._current_timestamp = 0.0
        self._update_count = 0
        self._gap_skip_count = 0

    def __repr__(self) -> str:
        return (
            f"MarketDynamicsCalculator(price={self._current_price:.2f}, "
            f"vel={self._current_velocity:.6f}, "
            f"energy={self._current_energy:.2f}, "
            f"samples={self._update_count}, "
            f"buffer={len(self._tick_history)})"
        )
