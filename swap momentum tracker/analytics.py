"""
实时特征工程引擎 (analytics.py)
================================
职责：
  1. 将原始 Tick 数据实时转化为多维量化特征，供 LLM 风控裁决
  2. 所有计算严格 O(1) 时间复杂度，零 pandas 依赖，纯 Python deque + 增量算法
  3. 零内存泄漏——全部缓冲区由 deque(maxlen=N) 自动淘汰

特征体系：
  ┌─────────────────────────────────────────────────────────────┐
  │  基础层    velocity / energy / raw_speed / gap_detection    │
  │  订单簿    spread / spread_pct / obi / vwap                 │
  │  标准化层  velocity_zscore / energy_zscore / is_extreme     │
  │  技术指标  tick_rsi / micro_volatility                      │
  │  聚合层    window_stats / recent_snapshot (for LLM)          │
  └─────────────────────────────────────────────────────────────┘

核心算法：
  • 滚动均值/方差 → 双累加器 O(1)（running sum + sum of squares + deque 滑动窗口）
  • RSI → Wilders平滑 O(1)（EMA近似，避免每Tick重算28个值）
  • VWAP → running sum(price×vol) / running sum(vol) O(1)
  • Z-Score → (x - μ) / σ 使用上述滚动统计

依赖注入：
  pipeline.py → calc.update(price, volume_usdt, timestamp, ask, bid, ...)
  返回值中的 zscore 等字段可直接被 llm_agent 消费
"""

from collections import deque
from typing import Dict, Any, Optional, List, Tuple


# ============================================================================
# 增量统计辅助类
# ============================================================================

class _RollingStats:
    """
    O(1) 滚动窗口统计——双累加器 + 环形队列实现。

    算法原理：
      sum   = 累加新值 - 淘汰旧值
      sum2  = 累加新值² - 淘汰旧值²
      mean  = sum / count
      var   = sum2/count - mean²  (总体方差)
      std   = √var
    """

    __slots__ = ("_window", "_sum", "_sum2", "_count")

    def __init__(self, window_size: int):
        self._window: deque = deque(maxlen=window_size)
        self._sum: float = 0.0
        self._sum2: float = 0.0
        self._count: int = 0

    def add(self, value: float) -> None:
        """O(1) 压入新值，满窗时自动淘汰最旧值。"""
        old = 0.0
        if len(self._window) == self._window.maxlen:
            old = self._window[0]

        self._window.append(value)
        self._sum += value - old
        self._sum2 += value * value - old * old
        self._count = len(self._window)

    @property
    def mean(self) -> float:
        if self._count == 0:
            return 0.0
        return self._sum / self._count

    @property
    def variance(self) -> float:
        if self._count < 2:
            return 0.0
        m = self.mean
        v = self._sum2 / self._count - m * m
        return max(0.0, v)  # 浮点误差保护

    @property
    def std(self) -> float:
        return self.variance ** 0.5

    @property
    def count(self) -> int:
        return self._count

    def zscore(self, value: float) -> float:
        """计算 value 在当前窗口内的 Z-Score。"""
        s = self.std
        if s < 1e-12:
            return 0.0
        return (value - self.mean) / s

    def reset(self) -> None:
        self._window.clear()
        self._sum = 0.0
        self._sum2 = 0.0
        self._count = 0


class _RSICalculator:
    """
    Tick 级 RSI 计算器——Wilder's Smoothing O(1) 实现。

    标准 RSI = 100 - 100/(1 + RS)
      RS = avg_gain / avg_loss
    每 Tick 用 EMA 方式平滑 avg_gain/avg_loss，避免 O(n) 窗口遍历。
    """

    __slots__ = ("_period", "_avg_gain", "_avg_loss", "_last_price", "_count")

    def __init__(self, period: int = 14):
        self._period = period
        self._avg_gain: float = 0.0
        self._avg_loss: float = 0.0
        self._last_price: Optional[float] = None
        self._count: int = 0

    def update(self, price: float) -> float:
        """O(1) 更新 RSI 值。"""
        if self._last_price is None:
            self._last_price = price
            return 50.0

        change = price - self._last_price
        self._last_price = price

        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        self._count += 1

        if self._count <= self._period:
            # Smoothed Moving Average 种子阶段
            self._avg_gain = (
                (self._avg_gain * (self._count - 1) + gain) / self._count
            )
            self._avg_loss = (
                (self._avg_loss * (self._count - 1) + loss) / self._count
            )
        else:
            # Wilder's Smoothing: α = 1/period
            alpha = 1.0 / self._period
            self._avg_gain = (gain - self._avg_gain) * alpha + self._avg_gain
            self._avg_loss = (loss - self._avg_loss) * alpha + self._avg_loss

        if self._avg_loss < 1e-12:
            return 100.0  # 纯多头
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def reset(self) -> None:
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._last_price = None
        self._count = 0


class _VWAPTracker:
    """
    滚动 VWAP 计算器——O(1) 累加器实现。

    VWAP = Σ(price × volume) / Σ(volume)
    """

    __slots__ = ("_window_pv", "_window_vol", "_sum_pv", "_sum_vol")

    def __init__(self, window_size: int = 100):
        self._window_pv: deque = deque(maxlen=window_size)   # price × volume
        self._window_vol: deque = deque(maxlen=window_size)   # volume

    def update(self, price: float, volume: float) -> float:
        """O(1) 更新 VWAP。"""
        pv = price * volume
        old_pv = 0.0
        old_vol = 0.0
        if len(self._window_pv) == self._window_pv.maxlen:
            old_pv = self._window_pv[0]
            old_vol = self._window_vol[0]

        self._window_pv.append(pv)
        self._window_vol.append(volume)

        # 增量维护
        if not hasattr(self, "_sum_pv"):
            self._sum_pv = 0.0
            self._sum_vol = 0.0
        self._sum_pv += pv - old_pv
        self._sum_vol += volume - old_vol

        if self._sum_vol < 1e-12:
            return price
        return self._sum_pv / self._sum_vol

    def reset(self) -> None:
        self._window_pv.clear()
        self._window_vol.clear()
        self._sum_pv = 0.0
        self._sum_vol = 0.0


# ============================================================================
# 实时特征工程引擎
# ============================================================================

class MarketDynamicsCalculator:
    """
    市场动力学实时特征计算器（单标的版）。

    每个合约实例化一个独立计算器：
      calculators = {"TSLA-USDT-SWAP": MarketDynamicsCalculator(s), ...}

    特征维度一览：
      - price, velocity, energy, raw_speed          (基础层)
      - spread, spread_pct, obi, vwap               (订单簿)
      - velocity_zscore, energy_zscore, is_extreme  (标准化层)
      - tick_rsi, micro_volatility                  (技术指标)
      - window_stats, recent_snapshot               (聚合层)
    """

    # ── 默认窗口参数 ──
    OBI_WINDOW = 5           # OBI 平滑窗口
    VWAP_WINDOW = 100        # VWAP 滚动窗口
    RSI_PERIOD = 14          # RSI 周期
    VOLATILITY_WINDOW = 20   # 微观波动率窗口
    ZSCORE_WINDOW = 600      # Z-Score 基准窗口（秒）→ 约 6000 ticks at 10Hz

    def __init__(self, settings):
        """初始化特征引擎。"""
        self.settings = settings
        self.alpha: float = settings.velocity_ema_alpha
        self.window: int = settings.energy_window
        self.maxlen: int = settings.history_maxlen
        self.gap_threshold: float = settings.gap_threshold_seconds

        # ── 完整历史环形缓冲区 ──
        self._tick_history: deque = deque(maxlen=self.maxlen)

        # ── 基础层状态 ──
        self._velocity_history: deque = deque(maxlen=self.window)
        self._energy_history: deque = deque(maxlen=self.window)
        self._ema_velocity: float = 0.0
        self._initialized: bool = False
        self._last_price: Optional[float] = None
        self._last_timestamp: Optional[float] = None

        # ── 订单簿特征 ──
        self._current_spread: float = 0.0
        self._current_spread_pct: float = 0.0
        self._current_obi: float = 0.0
        self._obi_rolling: deque = deque(maxlen=self.OBI_WINDOW)
        self._vwap = _VWAPTracker(self.VWAP_WINDOW)

        # ── Z-Score 滚动统计（双累加器 + 环形队列） ──
        self._vel_stats = _RollingStats(self.ZSCORE_WINDOW)
        self._eng_stats = _RollingStats(self.ZSCORE_WINDOW)
        self._current_vel_zscore: float = 0.0
        self._current_eng_zscore: float = 0.0

        # ── 技术指标 ──
        self._rsi = _RSICalculator(self.RSI_PERIOD)
        self._returns_history: deque = deque(maxlen=self.VOLATILITY_WINDOW)
        self._micro_vol: float = 0.0

        # ── 最新值缓存 ──
        self._current_price: float = 0.0
        self._current_volume: float = 0.0
        self._current_velocity: float = 0.0
        self._current_energy: float = 0.0
        self._current_raw_speed: float = 0.0
        self._current_timestamp: float = 0.0
        self._current_vwap: float = 0.0
        self._current_rsi: float = 50.0
        self._update_count: int = 0
        self._gap_skip_count: int = 0

        # 告警冷却（由 pipeline 设置）
        self._last_alert_ts: float = 0.0

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def price(self) -> float:       return self._current_price
    @property
    def velocity(self) -> float:    return self._current_velocity
    @property
    def energy(self) -> float:      return self._current_energy
    @property
    def spread(self) -> float:      return self._current_spread
    @property
    def spread_pct(self) -> float:  return self._current_spread_pct
    @property
    def obi(self) -> float:         return self._current_obi
    @property
    def vwap(self) -> float:        return self._current_vwap
    @property
    def vel_zscore(self) -> float:  return self._current_vel_zscore
    @property
    def eng_zscore(self) -> float:  return self._current_eng_zscore
    @property
    def tick_rsi(self) -> float:    return self._current_rsi
    @property
    def micro_volatility(self) -> float: return self._micro_vol
    @property
    def is_extreme(self) -> bool:   return abs(self._current_vel_zscore) > 3.0
    @property
    def is_initialized(self) -> bool: return self._initialized
    @property
    def sample_count(self) -> int:  return self._update_count
    @property
    def tick_count_in_buffer(self) -> int: return len(self._tick_history)
    @property
    def gap_skip_count(self) -> int: return self._gap_skip_count

    # ------------------------------------------------------------------
    # 核心数据更新（主入口）
    # ------------------------------------------------------------------

    def update(
        self,
        price: float,
        volume_usdt: float,
        timestamp: float,
        ask: float = 0.0,
        bid: float = 0.0,
        ask_sz: float = 0.0,
        bid_sz: float = 0.0,
    ) -> Dict[str, Any]:
        """
        喂入一个新的采样点，更新全部 15+ 维特征。O(1) 复杂度。

        Args:
            price:       最新成交价（USDT 本位）
            volume_usdt: 该笔成交的 USDT 价值
            timestamp:   UNIX 时间戳（秒）
            ask/bid:     买卖一价（可选，默认 0 跳过订单簿特征）
            ask_sz/bid_sz: 买卖一量（可选）

        Returns:
            完整特征快照字典
        """
        self._update_count += 1
        self._current_price = price
        self._current_volume = volume_usdt
        self._current_timestamp = timestamp

        # ── 基础层：速度 & 能量 ──
        is_gap = False
        if self._last_price is not None and self._last_timestamp is not None:
            dt = timestamp - self._last_timestamp
            dp = price - self._last_price

            if dt > self.gap_threshold:
                self._gap_skip_count += 1
                is_gap = True
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

                energy = abs(self._ema_velocity) * volume_usdt
                self._current_energy = energy
                self._energy_history.append((timestamp, energy))
        else:
            self._current_velocity = 0.0
            self._current_energy = 0.0
            self._current_raw_speed = 0.0

        # ── 追加历史 ──
        self._tick_history.append({
            "ts": timestamp, "price": price, "volume_usdt": volume_usdt,
            "velocity": self._current_velocity, "energy": self._current_energy,
            "raw_speed": self._current_raw_speed,
        })

        # ── 订单簿特征（仅当 ask/bid 均有效且非跳空时计算） ──
        if ask > 0 and bid > 0 and not is_gap:
            self._current_spread = ask - bid
            self._current_spread_pct = (self._current_spread / price * 100.0
                                         if price > 0 else 0.0)

            # OBI: (bid_sz - ask_sz) / (bid_sz + ask_sz)
            total_sz = bid_sz + ask_sz
            if total_sz > 0:
                obi_raw = (bid_sz - ask_sz) / total_sz
            else:
                obi_raw = 0.0
            # 简单移动平均平滑
            self._obi_rolling.append(obi_raw)
            self._current_obi = (
                sum(self._obi_rolling) / len(self._obi_rolling)
            )

            # VWAP
            self._current_vwap = self._vwap.update(price, volume_usdt if volume_usdt > 0 else 1.0)

        # ── Z-Score 标准化（非跳空时更新统计窗口） ──
        if not is_gap and self._current_velocity != 0:
            self._vel_stats.add(self._current_velocity)
            self._eng_stats.add(self._current_energy)
            self._current_vel_zscore = self._vel_stats.zscore(self._current_velocity)
            self._current_eng_zscore = self._eng_stats.zscore(self._current_energy)

        # ── 技术指标 ──
        # RSI
        self._current_rsi = self._rsi.update(price)

        # 微观波动率（价格回报率标准差）
        if self._last_price is not None and self._last_price > 0 and not is_gap:
            ret = (price - self._last_price) / self._last_price
            self._returns_history.append(ret)
            self._micro_vol = self._compute_rolling_std(self._returns_history)

        # ── 更新上一采样点 ──
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
            # 订单簿
            "spread": self._current_spread,
            "spread_pct": self._current_spread_pct,
            "obi": self._current_obi,
            "vwap": self._current_vwap,
            # Z-Score
            "velocity_zscore": self._current_vel_zscore,
            "energy_zscore": self._current_eng_zscore,
            "is_extreme": self.is_extreme,
            # 技术指标
            "tick_rsi": self._current_rsi,
            "micro_volatility": self._micro_vol,
        }

    # ------------------------------------------------------------------
    # 窗口查询（供 reporter.py 使用）
    # ------------------------------------------------------------------

    def get_window_stats(self, since_ts: float) -> Optional[Dict[str, Any]]:
        """获取自 since_ts 以来的聚合统计（向后兼容）。"""
        window = [t for t in self._tick_history if t["ts"] >= since_ts]
        if len(window) < 2:
            return None

        velocities = [t["velocity"] for t in window]
        energies = [t["energy"] for t in window]
        times = [t["ts"] for t in window]
        prices = [t["price"] for t in window]
        volumes = [t["volume_usdt"] for t in window]
        n = len(window)

        avg_velocity = sum(velocities) / n
        variance = sum((v - avg_velocity) ** 2 for v in velocities) / n
        std_velocity = variance ** 0.5

        energy_integral = 0.0
        for i in range(1, n):
            dt_i = times[i] - times[i - 1]
            if dt_i > 0:
                energy_integral += (energies[i] + energies[i - 1]) / 2.0 * dt_i

        avg_energy = sum(energies) / n
        max_energy = max(energies)
        max_idx = energies.index(max_energy)
        min_energy = min(energies)
        min_idx = energies.index(min_energy)

        max_velocity = max(velocities)
        max_vel_idx = velocities.index(max_velocity)
        min_velocity = min(velocities)
        min_vel_idx = velocities.index(min_velocity)

        first_price = window[0]["price"]
        last_price = window[-1]["price"]
        price_change_pct = (
            (last_price - first_price) / first_price * 100.0
            if first_price > 0 else 0.0
        )

        total_volume = sum(volumes)
        net_velocity = sum(velocities)
        if net_velocity > 0.001:
            direction = "买盘主导 (偏多头)"
        elif net_velocity < -0.001:
            direction = "卖盘主导 (偏空头)"
        else:
            direction = "多空均衡 (震荡)"

        pos_energy = sum(energies[i] for i in range(n) if velocities[i] > 0)
        neg_energy = sum(energies[i] for i in range(n) if velocities[i] < 0)
        total_energy = pos_energy + neg_energy
        bull_ratio = (pos_energy / total_energy * 100.0) if total_energy > 0 else 50.0

        return {
            "start_time": times[0], "end_time": times[-1],
            "sample_count": n, "first_price": first_price,
            "last_price": last_price, "price_change_pct": price_change_pct,
            "total_volume": total_volume,
            "avg_velocity": avg_velocity, "std_velocity": std_velocity,
            "max_velocity": max_velocity, "max_velocity_ts": times[max_vel_idx],
            "min_velocity": min_velocity, "min_velocity_ts": times[min_vel_idx],
            "energy_integral": energy_integral, "avg_energy": avg_energy,
            "max_energy": max_energy, "max_energy_ts": times[max_idx],
            "max_energy_price": window[max_idx]["price"],
            "max_energy_velocity": window[max_idx]["velocity"],
            "min_energy": min_energy, "min_energy_ts": times[min_idx],
            "min_energy_price": window[min_idx]["price"],
            "min_energy_velocity": window[min_idx]["velocity"],
            "direction": direction, "net_velocity": net_velocity,
            "bull_ratio": bull_ratio,
        }

    def get_recent_snapshot(self, n: int = 10) -> Dict[str, Any]:
        """
        获取当前完整特征快照，供 LLM 风控裁决使用。

        返回结构包含所有维度的实时值 + 近期趋势摘要，
        使 LLM 能基于 Z-Score、OBI、微观波动率等高维特征进行判断。
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

        # ── 极端程度判定 ──
        if abs(self._current_vel_zscore) > 4.0:
            severity = "extreme"
        elif abs(self._current_vel_zscore) > 2.5:
            severity = "significant"
        elif abs(self._current_vel_zscore) > 1.5:
            severity = "moderate"
        else:
            severity = "normal"

        # ── OBI 解读 ──
        if self._current_obi > 0.3:
            obi_signal = "买方深度压倒性占优，潜在向上突破"
        elif self._current_obi < -0.3:
            obi_signal = "卖方深度压倒性占优，潜在向下突破"
        elif self._current_obi > 0.1:
            obi_signal = "买方略占优势"
        elif self._current_obi < -0.1:
            obi_signal = "卖方略占优势"
        else:
            obi_signal = "买卖均衡"

        return {
            # 基础层
            "latest": ticks[-1],
            "recent_velocities": [(t["ts"], t["velocity"]) for t in ticks[-5:]],
            "recent_energies": [(t["ts"], t["energy"]) for t in ticks[-5:]],
            "velocity_mean": v_mean,
            "energy_mean": e_mean,
            "trend": trend,
            "sample_count": len(ticks),
            # 订单簿层
            "spread": self._current_spread,
            "spread_pct": self._current_spread_pct,
            "obi": self._current_obi,
            "obi_signal": obi_signal,
            "vwap": self._current_vwap,
            "price_vs_vwap_pct": (
                (self._current_price - self._current_vwap) / self._current_vwap * 100.0
                if self._current_vwap > 0 else 0.0
            ),
            # 标准化层
            "velocity_zscore": self._current_vel_zscore,
            "energy_zscore": self._current_eng_zscore,
            "velocity_zscore_mean": self._vel_stats.mean,
            "velocity_zscore_std": self._vel_stats.std,
            "is_extreme": self.is_extreme,
            "severity": severity,
            # 技术指标层
            "tick_rsi": self._current_rsi,
            "micro_volatility": self._micro_vol,
            "rsi_signal": (
                "超买" if self._current_rsi > 70 else
                "超卖" if self._current_rsi < 30 else "中性"
            ),
        }

    # ------------------------------------------------------------------
    # 实时看板统计（向后兼容）
    # ------------------------------------------------------------------

    def get_velocity_stats(self) -> Dict[str, float]:
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
        """重置所有内部状态（断线重连后使用）。"""
        self._tick_history.clear()
        self._velocity_history.clear()
        self._energy_history.clear()
        self._obi_rolling.clear()
        self._returns_history.clear()
        self._ema_velocity = 0.0
        self._initialized = False
        self._last_price = None
        self._last_timestamp = None
        self._current_price = 0.0
        self._current_volume = 0.0
        self._current_velocity = 0.0
        self._current_energy = 0.0
        self._current_raw_speed = 0.0
        self._current_spread = 0.0
        self._current_spread_pct = 0.0
        self._current_obi = 0.0
        self._current_vwap = 0.0
        self._current_vel_zscore = 0.0
        self._current_eng_zscore = 0.0
        self._current_rsi = 50.0
        self._micro_vol = 0.0
        self._current_timestamp = 0.0
        self._update_count = 0
        self._gap_skip_count = 0
        self._vwap.reset()
        self._rsi.reset()
        self._vel_stats.reset()
        self._eng_stats.reset()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_rolling_std(deq: deque) -> float:
        """O(n) 计算 deque 内值的标准差（仅用于小窗口 < 50）。"""
        n = len(deq)
        if n < 2:
            return 0.0
        mean = sum(deq) / n
        variance = sum((x - mean) ** 2 for x in deq) / n
        return max(0.0, variance) ** 0.5

    def __repr__(self) -> str:
        return (
            f"MarketDynamicsCalculator("
            f"price={self._current_price:.2f}, "
            f"vel={self._current_velocity:.4f}(z={self._current_vel_zscore:.2f}), "
            f"rsi={self._current_rsi:.1f}, "
            f"obi={self._current_obi:.4f}, "
            f"samples={self._update_count})"
        )
