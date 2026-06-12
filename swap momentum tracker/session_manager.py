"""
时区与盘口过滤模块 (session_manager.py)
=========================================
职责：
  1. 利用 pytz 处理时区转换（UTC ↔ KST ↔ US Eastern）
  2. 实现状态机，判断当前底层现货市场盘口：
     - KRX_TRADING    : 韩股交易中（09:00-15:30 KST）
     - US_PREMARKET   : 美股盘前（04:00-09:30 ET）
     - US_REGULAR     : 美股盘中（09:30-16:00 ET）
     - US_AFTERHOURS  : 美股盘后（16:00-20:00 ET）
     - ALL_CLOSED      : 全休市
  3. 核心作用：底层现货休市期间，拦截低流动性下的伪动能信号
     —— 返回空标的列表，阻止 Consumer 处理非活跃合约数据
  4. 提供动态获取当前应订阅 USDT-SWAP 合约列表的接口

时区对照：
  KST = UTC+9 (固定，无夏令时)
  EST = UTC-5 (冬令时) / EDT = UTC-4 (夏令时)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum, auto
from typing import List, Optional

import pytz
from pytz import timezone as tz

from config import Settings

logger = logging.getLogger("SwapMomentum.SessionManager")


class SessionState(Enum):
    """底层现货市场盘口状态"""
    KRX_TRADING = auto()
    US_PREMARKET = auto()
    US_REGULAR = auto()
    US_AFTERHOURS = auto()
    ALL_CLOSED = auto()


@dataclass
class SessionInfo:
    """当前盘口信息"""
    state: SessionState
    state_name: str
    active_swaps: List[str]        # 当前应监控的合约代码
    suppressed_swaps: List[str]     # 当前被抑制的合约代码（底层休市）
    next_state: Optional[SessionState] = None
    next_transition_utc: Optional[datetime] = None


class MarketSession:
    """
    全球底层现货市场盘口调度器。

    虽然加密交易所 7×24 小时无休，但股权代币合约的流动性
    高度依赖于底层现货市场是否开市。在现货休市期间：
      - 合约仍然可能有零星成交，但流动性极低
      - 此时的价格波动往往是"伪动能"——假的资金信号
      - 系统必须过滤这些信号，避免 LLM 被误导

    本模块的输出直接驱动 data_fetcher 的订阅列表和 consumer 的过滤逻辑。
    """

    US_PREMARKET_START = time(4, 0)
    US_REGULAR_START = time(9, 30)
    US_REGULAR_END = time(16, 0)
    US_AFTER_END = time(20, 0)

    KRX_START = time(9, 0)
    KRX_END = time(15, 30)

    def __init__(self, settings: Settings):
        self.settings = settings
        self.krx_swaps: List[str] = list(settings.krx_swaps)
        self.us_swaps: List[str] = list(settings.us_swaps)

        self._utc = pytz.UTC
        self._kst: pytz.BaseTzInfo = tz("Asia/Seoul")
        self._us_eastern: pytz.BaseTzInfo = tz("US/Eastern")

        self._previous_state: Optional[SessionState] = None
        self._previous_active: List[str] = []
        self._on_transition_callbacks: list = []

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def current_session(self, at_time: Optional[datetime] = None) -> SessionInfo:
        """
        获取指定时刻的盘口信息。

        Args:
            at_time: 查询时刻（UTC），None = 当前时刻

        Returns:
            SessionInfo —— 活跃合约列表 + 抑制合约列表 + 状态
        """
        utc_now = at_time if at_time else datetime.now(self._utc)

        # 分别转换为本地时区 datetime（含日期），用于独立判定各市场周末
        krx_dt = utc_now.astimezone(self._kst)
        us_dt = utc_now.astimezone(self._us_eastern)

        krx_time = krx_dt.time()
        us_time = us_dt.time()

        # 韩股：基于首尔时间独立判断是否交易日
        krx_is_weekend = krx_dt.weekday() >= 5
        # 美股：基于美东时间独立判断是否交易日
        us_is_weekend = us_dt.weekday() >= 5

        # 韩股活跃判定：在交易时段内 且 当天是工作日
        krx_active = (
            (not krx_is_weekend)
            and (self.KRX_START <= krx_time <= self.KRX_END)
        )
        # 美股盘口判定：在交易时段内 且 当天是工作日
        us_state = (
            self._get_us_session(us_time)
            if not us_is_weekend
            else SessionState.ALL_CLOSED
        )

        if us_state == SessionState.US_REGULAR:
            info = SessionInfo(
                state=SessionState.US_REGULAR,
                state_name="美股常规交易",
                active_swaps=list(self.us_swaps),
                suppressed_swaps=list(self.krx_swaps),
                next_state=SessionState.US_AFTERHOURS,
                next_transition_utc=self._next_us_transition(us_dt, SessionState.US_REGULAR),
            )
        elif us_state == SessionState.US_PREMARKET:
            info = SessionInfo(
                state=SessionState.US_PREMARKET,
                state_name="美股盘前交易",
                active_swaps=list(self.us_swaps),
                suppressed_swaps=list(self.krx_swaps),
                next_state=SessionState.US_REGULAR,
                next_transition_utc=self._next_us_transition(us_dt, SessionState.US_PREMARKET),
            )
        elif us_state == SessionState.US_AFTERHOURS:
            info = SessionInfo(
                state=SessionState.US_AFTERHOURS,
                state_name="美股盘后交易",
                active_swaps=list(self.us_swaps),
                suppressed_swaps=list(self.krx_swaps),
                next_state=SessionState.ALL_CLOSED,
                next_transition_utc=self._next_us_transition(us_dt, SessionState.US_AFTERHOURS),
            )
        elif krx_active:
            info = SessionInfo(
                state=SessionState.KRX_TRADING,
                state_name="韩股交易中",
                active_swaps=list(self.krx_swaps),
                suppressed_swaps=list(self.us_swaps),
                next_state=SessionState.ALL_CLOSED,
                next_transition_utc=self._next_krx_transition(utc_now),
            )
        else:
            info = SessionInfo(
                state=SessionState.ALL_CLOSED,
                state_name="全休市（非交易时段）",
                active_swaps=[],
                suppressed_swaps=self.krx_swaps + self.us_swaps,
            )

        self._check_transition(info)
        return info

    @property
    def previous_state(self) -> Optional[SessionState]:
        return self._previous_state

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_us_session(self, us_time: time) -> SessionState:
        if self.US_REGULAR_START <= us_time < self.US_REGULAR_END:
            return SessionState.US_REGULAR
        elif self.US_PREMARKET_START <= us_time < self.US_REGULAR_START:
            return SessionState.US_PREMARKET
        elif self.US_REGULAR_END <= us_time < self.US_AFTER_END:
            return SessionState.US_AFTERHOURS
        return SessionState.ALL_CLOSED

    def _next_us_transition(self, us_dt: datetime, current: SessionState) -> Optional[datetime]:
        us_date = us_dt.date()
        if current == SessionState.US_PREMARKET:
            next_et = self._us_eastern.localize(datetime.combine(us_date, self.US_REGULAR_START))
        elif current == SessionState.US_REGULAR:
            next_et = self._us_eastern.localize(datetime.combine(us_date, self.US_REGULAR_END))
        elif current == SessionState.US_AFTERHOURS:
            next_et = self._us_eastern.localize(datetime.combine(us_date, self.US_AFTER_END))
        else:
            return None
        return next_et.astimezone(self._utc)

    def _next_krx_transition(self, utc_now: datetime) -> Optional[datetime]:
        krx_dt = utc_now.astimezone(self._kst)
        krx_date = krx_dt.date()
        krx_time_val = krx_dt.time()

        if krx_time_val < self.KRX_START:
            next_kst = self._kst.localize(datetime.combine(krx_date, self.KRX_START))
        elif krx_time_val < self.KRX_END:
            next_kst = self._kst.localize(datetime.combine(krx_date, self.KRX_END))
        else:
            next_date = krx_date + timedelta(days=1)
            while next_date.weekday() >= 5:
                next_date += timedelta(days=1)
            next_kst = self._kst.localize(datetime.combine(next_date, self.KRX_START))
        return next_kst.astimezone(self._utc)

    def _check_transition(self, info: SessionInfo) -> None:
        """检测盘口切换并触发回调。"""
        if self._previous_state is not None and (
            info.state != self._previous_state
            or set(info.active_swaps) != set(self._previous_active)
        ):
            logger.info(
                f"盘口切换 | {self._state_name(self._previous_state)} → {info.state_name} | "
                f"活跃={info.active_swaps} | 抑制={info.suppressed_swaps}"
            )
        self._previous_state = info.state
        self._previous_active = list(info.active_swaps)

    @staticmethod
    def _state_name(state: SessionState) -> str:
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
            f"active={info.active_swaps}, "
            f"suppressed={len(info.suppressed_swaps)})"
        )
