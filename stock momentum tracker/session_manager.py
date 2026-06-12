"""
时区与盘口调度模块 (session_manager.py)
=========================================
职责：
  1. 利用 pytz 处理复杂的时区转换（UTC ↔ KST ↔ US Eastern）
  2. 实现状态机，精确判断当前所处时段：
     - KRX_TRADING    : 韩股交易中（09:00-15:30 KST）
     - US_PREMARKET   : 美股盘前（04:00-09:30 ET）
     - US_REGULAR     : 美股盘中（09:30-16:00 ET）
     - US_AFTERHOURS  : 美股盘后（16:00-20:00 ET）
     - ALL_CLOSED      : 全休市
  3. 提供接口供主程序动态获取当前应监控的标的列表
  4. 休市期间返回空列表，停止 API 请求，节省配额

时区对照：
  KST = UTC+9 (固定，无夏令时)
  EST = UTC-5 (冬令时，11月初~3月中)
  EDT = UTC-4 (夏令时，3月中~11月初)

北京时间 (CST) = UTC+8 = KST-1
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum, auto
from typing import List, Optional, Tuple

import pytz
from pytz import timezone as tz

from config import Settings, Ansi

logger = logging.getLogger("StockMomentum.SessionManager")


# ============================================================================
# 盘口状态枚举
# ============================================================================

class SessionState(Enum):
    """全球市场盘口状态"""
    KRX_TRADING = auto()       # 韩股交易中
    US_PREMARKET = auto()      # 美股盘前交易
    US_REGULAR = auto()        # 美股常规交易
    US_AFTERHOURS = auto()     # 美股盘后交易
    ALL_CLOSED = auto()        # 全休市


# 韩股午休时间
KRX_LUNCH_START = time(11, 30)  # KST
KRX_LUNCH_END = time(12, 30)    # KST


# ============================================================================
# 盘口会话数据类
# ============================================================================

@dataclass
class SessionInfo:
    """当前盘口信息"""
    state: SessionState
    state_name: str
    active_symbols: List[str]
    next_state: Optional[SessionState] = None
    next_transition_utc: Optional[datetime] = None


# ============================================================================
# 盘口调度器
# ============================================================================

class MarketSession:
    """
    全球市场盘口调度器。

    内部维护时区对象（UTC、KST、US Eastern），
    根据当前 UTC 时间判定盘口状态并返回应监控的标的列表。

    使用方法：
        session = MarketSession(settings)
        info = session.current_session()
        if info.active_symbols:
            # 有标的在交易中，启动数据获取
        else:
            # 全休市，休眠
    """

    # US 股市关键时间（ET 时区）
    US_PREMARKET_START = time(4, 0)    # 04:00 ET
    US_PREMARKET_END = time(9, 30)     # 09:30 ET
    US_REGULAR_START = time(9, 30)     # 09:30 ET
    US_REGULAR_END = time(16, 0)       # 16:00 ET
    US_AFTER_END = time(20, 0)         # 20:00 ET

    # KRX 关键时间（KST）
    KRX_START = time(9, 0)             # 09:00 KST
    KRX_END = time(15, 30)             # 15:30 KST

    def __init__(self, settings: Settings):
        """
        初始化盘口调度器。

        Args:
            settings: 系统配置实例
        """
        self.settings = settings
        self.krx_symbols: List[str] = list(settings.krx_symbols)
        self.us_symbols: List[str] = list(settings.us_symbols)

        # 时区对象
        self._utc = pytz.UTC
        self._kst: pytz.BaseTzInfo = tz("Asia/Seoul")
        self._us_eastern: pytz.BaseTzInfo = tz("US/Eastern")  # 自动处理 EST/EDT

        # 上一状态（用于变更检测）
        self._previous_state: Optional[SessionState] = None

        # 盘口切换回调列表
        self._on_transition_callbacks: list = []

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def current_session(self, at_time: Optional[datetime] = None) -> SessionInfo:
        """
        获取指定时刻的盘口信息。

        Args:
            at_time: 查询时刻（UTC），None 表示当前时刻

        Returns:
            SessionInfo —— 包含状态、活跃标的、下次切换时间
        """
        utc_now = at_time if at_time else datetime.now(self._utc)
        day_of_week = utc_now.weekday()  # 0=Mon, 6=Sun

        # 周末全部休市
        if day_of_week >= 5:
            info = SessionInfo(
                state=SessionState.ALL_CLOSED,
                state_name="全休市（周末）",
                active_symbols=[],
            )
            self._check_transition(info)
            return info

        # 转换为当地时区
        krx_time = utc_now.astimezone(self._kst).time()
        us_time = utc_now.astimezone(self._us_eastern).time()
        us_dt = utc_now.astimezone(self._us_eastern)

        # 判断 KRX 是否在交易（含午休判定）
        krx_active = self._is_krx_active(krx_time)

        # 判断 US 盘口
        us_state = self._get_us_session_state(us_time)

        # 综合判定
        if us_state == SessionState.US_REGULAR:
            info = SessionInfo(
                state=SessionState.US_REGULAR,
                state_name="美股常规交易",
                active_symbols=list(self.us_symbols),
                next_state=SessionState.US_AFTERHOURS,
                next_transition_utc=self._next_us_transition(
                    us_dt, SessionState.US_REGULAR
                ),
            )
        elif us_state == SessionState.US_PREMARKET:
            info = SessionInfo(
                state=SessionState.US_PREMARKET,
                state_name="美股盘前交易",
                active_symbols=list(self.us_symbols),
                next_state=SessionState.US_REGULAR,
                next_transition_utc=self._next_us_transition(
                    us_dt, SessionState.US_PREMARKET
                ),
            )
        elif us_state == SessionState.US_AFTERHOURS:
            info = SessionInfo(
                state=SessionState.US_AFTERHOURS,
                state_name="美股盘后交易",
                active_symbols=list(self.us_symbols),
                next_state=SessionState.ALL_CLOSED,
                next_transition_utc=self._next_us_transition(
                    us_dt, SessionState.US_AFTERHOURS
                ),
            )
        elif krx_active:
            info = SessionInfo(
                state=SessionState.KRX_TRADING,
                state_name="韩股交易中",
                active_symbols=list(self.krx_symbols),
                next_state=SessionState.ALL_CLOSED,
                next_transition_utc=self._next_krx_transition(utc_now),
            )
        else:
            info = SessionInfo(
                state=SessionState.ALL_CLOSED,
                state_name="全休市（非交易时段）",
                active_symbols=[],
            )

        self._check_transition(info)
        return info

    def add_transition_callback(self, callback):
        """
        注册盘口切换回调函数。

        当 active_symbols 列表发生变化时触发。
        callback(previous_state: SessionState, new_state: SessionState, new_symbols: List[str])
        """
        self._on_transition_callbacks.append(callback)

    @property
    def previous_state(self) -> Optional[SessionState]:
        """上一盘口状态"""
        return self._previous_state

    # ------------------------------------------------------------------
    # 内部：状态判定
    # ------------------------------------------------------------------

    def _is_krx_active(self, krx_time: time) -> bool:
        """
        判断韩股当前是否处于交易时段。

        KRX 交易时间：09:00-15:30 KST（午休 11:30-12:30 仍算作交易日，数据会断流）
        """
        return self.KRX_START <= krx_time <= self.KRX_END

    def _is_krx_lunch(self, krx_time: time) -> bool:
        """判断是否处于韩股午休时段。"""
        return KRX_LUNCH_START <= krx_time <= KRX_LUNCH_END

    def _get_us_session_state(self, us_time: time) -> SessionState:
        """根据 US Eastern 时间判定美股盘口状态。"""
        if self.US_REGULAR_START <= us_time < self.US_REGULAR_END:
            return SessionState.US_REGULAR
        elif self.US_PREMARKET_START <= us_time < self.US_PREMARKET_END:
            return SessionState.US_PREMARKET
        elif self.US_REGULAR_END <= us_time < self.US_AFTER_END:
            return SessionState.US_AFTERHOURS
        else:
            return SessionState.ALL_CLOSED

    # ------------------------------------------------------------------
    # 内部：下次切换时间计算
    # ------------------------------------------------------------------

    def _next_us_transition(
        self, us_dt: datetime, current: SessionState
    ) -> Optional[datetime]:
        """
        计算美股盘口的下一次状态切换 UTC 时间。

        Args:
            us_dt:   当前 US Eastern 时间
            current: 当前美股盘口状态

        Returns:
            下次切换时刻（UTC）
        """
        us_date = us_dt.date()

        if current == SessionState.US_PREMARKET:
            next_et = self._us_eastern.localize(
                datetime.combine(us_date, self.US_REGULAR_START)
            )
        elif current == SessionState.US_REGULAR:
            next_et = self._us_eastern.localize(
                datetime.combine(us_date, self.US_REGULAR_END)
            )
        elif current == SessionState.US_AFTERHOURS:
            next_et = self._us_eastern.localize(
                datetime.combine(us_date, self.US_AFTER_END)
            )
        else:
            return None

        return next_et.astimezone(self._utc)

    def _next_krx_transition(self, utc_now: datetime) -> Optional[datetime]:
        """
        计算 KRX 盘口的下一次状态切换 UTC 时间。

        KRX 15:30 KST 收盘 → 次日 09:00 KST 开盘
        """
        krx_dt = utc_now.astimezone(self._kst)
        krx_date = krx_dt.date()
        krx_time = krx_dt.time()

        if krx_time < self.KRX_START:
            # 尚未开盘，下次切换 = 今日开盘
            next_kst = self._kst.localize(
                datetime.combine(krx_date, self.KRX_START)
            )
        elif krx_time < self.KRX_END:
            # 交易中，下次切换 = 今日收盘
            next_kst = self._kst.localize(
                datetime.combine(krx_date, self.KRX_END)
            )
        else:
            # 已收盘，下次切换 = 次日开盘（跳过周末）
            next_date = krx_date + timedelta(days=1)
            while next_date.weekday() >= 5:
                next_date += timedelta(days=1)
            next_kst = self._kst.localize(
                datetime.combine(next_date, self.KRX_START)
            )

        return next_kst.astimezone(self._utc)

    # ------------------------------------------------------------------
    # 内部：状态切换检测
    # ------------------------------------------------------------------

    def _check_transition(self, info: SessionInfo) -> None:
        """检测盘口是否发生切换，若切换则触发回调。"""
        if self._previous_state is not None and info.state != self._previous_state:
            logger.info(
                f"盘口切换 | {self._state_name(self._previous_state)} → "
                f"{info.state_name} | 活跃标的={len(info.active_symbols)}"
            )
            for cb in self._on_transition_callbacks:
                try:
                    cb(self._previous_state, info.state, info.active_symbols)
                except Exception:
                    logger.exception("盘口切换回调执行异常")

        self._previous_state = info.state

    @staticmethod
    def _state_name(state: SessionState) -> str:
        """将枚举转为中文名称。"""
        mapping = {
            SessionState.KRX_TRADING: "韩股交易",
            SessionState.US_PREMARKET: "美股盘前",
            SessionState.US_REGULAR: "美股常规",
            SessionState.US_AFTERHOURS: "美股盘后",
            SessionState.ALL_CLOSED: "全休市",
        }
        return mapping.get(state, "未知")

    def __repr__(self) -> str:
        info = self.current_session()
        return (
            f"MarketSession(state={info.state_name}, "
            f"symbols={info.active_symbols}, "
            f"next={info.next_transition_utc})"
        )
