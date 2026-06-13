"""
配置模块 (config.py)
=====================
职责：
  1. 管理日间/夜间股权代币永续合约标的池（USDT 本位 SWAP 格式）
  2. 配置 OKX V5 交易所公共 WebSocket 端点
  3. 配置 LLM 大模型 API 端点与密钥
  4. 设定能量爆发阈值，用于触发 LLM 裁判
  5. 配置双通道日志系统（控制台着色 + 轮转文件）

所有敏感信息支持从环境变量或 .env 文件读取。
"""

import os
import sys
import ctypes
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, List


def _enable_windows_ansi() -> None:
    """为 Windows 控制台启用 ANSI 转义序列支持。"""
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= 0x0004
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


_enable_windows_ansi()


class Ansi:
    """ANSI 转义序列常量。"""
    RESET: ClassVar[str] = "\033[0m"
    BOLD: ClassVar[str] = "\033[1m"
    RED: ClassVar[str] = "\033[91m"
    GREEN: ClassVar[str] = "\033[92m"
    YELLOW: ClassVar[str] = "\033[93m"
    BLUE: ClassVar[str] = "\033[94m"
    MAGENTA: ClassVar[str] = "\033[95m"
    CYAN: ClassVar[str] = "\033[96m"
    BOLD_RED: ClassVar[str] = "\033[1;91m"
    BOLD_YELLOW: ClassVar[str] = "\033[1;93m"
    BOLD_GREEN: ClassVar[str] = "\033[1;92m"
    BOLD_CYAN: ClassVar[str] = "\033[1;96m"


class ColoredConsoleFormatter(logging.Formatter):
    """控制台日志按级别着色。"""
    LEVEL_COLORS: ClassVar[dict] = {
        logging.WARNING: Ansi.BOLD_YELLOW,
        logging.ERROR: Ansi.BOLD_RED,
        logging.CRITICAL: Ansi.BOLD_RED,
        logging.DEBUG: "\033[90m",
    }

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        color = self.LEVEL_COLORS.get(record.levelno, "")
        if color:
            return f"{color}{formatted}{Ansi.RESET}"
        return formatted


# ============================================================================
# 全局配置数据类
# ============================================================================

@dataclass
class Settings:
    """
    股权代币永续合约动量监控系统配置。

    所有参数均可通过环境变量或 get_settings() 覆盖。
    """

    # ---- 日间标的池：韩股底层代币化合约 ----
    krx_swaps: List[str] = field(default_factory=lambda: [
        "SAMSUNG-USDT-SWAP",    # 三星电子
        "SKHYNIX-USDT-SWAP",    # SK 海力士
    ])

    # ---- 夜间标的池：美股底层代币化合约 ----
    us_swaps: List[str] = field(default_factory=lambda: [
        "TSLA-USDT-SWAP",       # 特斯拉
        "NVDA-USDT-SWAP",       # 英伟达
        "AAPL-USDT-SWAP",       # Apple
        "MU-USDT-SWAP",         # 美光科技
        "WDC-USDT-SWAP",        # 西部数据
        "MRVL-USDT-SWAP",       # Marvell
    ])

    # ---- OKX V5 交易所端点 ----
    okx_ws_public_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    """OKX 公共 WebSocket 端点（无需 API Key）"""

    okx_ws_private_url: str = "wss://ws.okx.com:8443/ws/v5/private"
    """OKX 私有 WebSocket 端点（如需订阅私有频道）"""

    # ---- WebSocket 连接参数 ----
    ws_ping_interval: int = 20
    """WebSocket 协议级 Ping 间隔（秒）"""

    ws_ping_timeout: int = 10
    """等待 Pong 响应的超时时间（秒）"""

    ws_reconnect_min_delay: float = 1.0
    """指数退避重连起始延迟（秒）"""

    ws_reconnect_max_delay: float = 60.0
    """指数退避重连封顶延迟（秒）"""

    ws_reconnect_backoff_factor: float = 2.0
    """指数退避底数因子"""

    ws_max_reconnect_attempts: int = 0
    """最大重连次数，0=无限重连"""

    # ---- LLM 大模型配置（OpenAI 兼容） ----
    llm_api_endpoint: str = "https://api.openai.com/v1/chat/completions"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 10.0
    """LLM 请求超时熔断时间（秒）"""

    llm_max_tokens: int = 300
    llm_temperature: float = 0.2
    llm_cooldown_seconds: int = 60
    """同标的 LLM 分析冷却期（秒）"""

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_endpoint and self.llm_api_key)

    # ---- 量化指标参数 ----
    velocity_ema_alpha: float = 0.15
    """速度 EMA 平滑系数"""

    energy_window: int = 20
    """能量统计窗口（采样点数）"""

    history_maxlen: int = 5000
    """环形缓冲区最大长度"""

    gap_threshold_seconds: float = 300.0
    """
    跳空检测阈值（秒）。
    若两 Tick 间隔超过此值（如底层现货收盘期间），
    视为新会话，不计算该跨度的速度。
    """

    # ---- 能量告警阈值 ----
    energy_threshold: float = 5000.0
    """
    能量爆发阈值。
    当瞬时能量超过此值时触发 LLM 风控分析。
    股权代币合约流动性远低于主流币，阈值应相应调低。
    """

    alert_cooldown_seconds: int = 30
    """告警冷却期（秒）"""

    # ---- 5 分钟周期报告 ----
    report_interval_seconds: int = 300
    report_dir: str = "reports"
    report_filename_template: str = "Swap_Momentum_{timestamp}.txt"

    # ---- 日志配置 ----
    log_dir: str = "logs"
    log_file: str = "swap_momentum.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 7
    log_level: str = "INFO"

    # ---- 运行控制 ----
    shutdown_timeout: float = 5.0
    stats_interval: float = 120.0
    session_check_interval: float = 30.0

    # ---- 交易/OMS 配置 ----
    trading_mode: str = "OFF"
    """
    交易模式：
      OFF   = 只读模式（默认，不发送任何订单）
      PAPER = Alpaca 模拟盘（美股碎股）
      LIVE  = OKX 实盘（USDT 本位永续合约）
    """

    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""

    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""

    default_notional_value: float = 1000.0
    """默认名义价值（USD），用于计算下单数量"""

    @property
    def trading_enabled(self) -> bool:
        return self.trading_mode in ("PAPER", "LIVE")


# ============================================================================
# 日志系统
# ============================================================================

def setup_logging(settings: Settings) -> logging.Logger:
    """配置双通道日志系统。"""
    logger = logging.getLogger("SwapMomentum")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    console_formatter = ColoredConsoleFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    file_formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | "
            "%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        filename=str(log_dir / settings.log_file),
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger


# ============================================================================
# 配置工厂
# ============================================================================

def get_settings(**kwargs) -> Settings:
    """创建配置实例，支持环境变量覆盖。"""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass

    env_overrides: dict = {}

    str_envs = {
        "LLM_API_KEY": "llm_api_key",
        "LLM_API_ENDPOINT": "llm_api_endpoint",
        "LLM_MODEL": "llm_model",
        "TRADING_MODE": "trading_mode",
        "ALPACA_API_KEY": "alpaca_api_key",
        "ALPACA_API_SECRET": "alpaca_api_secret",
        "OKX_API_KEY": "okx_api_key",
        "OKX_API_SECRET": "okx_api_secret",
        "OKX_PASSPHRASE": "okx_passphrase",
    }
    for env_key, attr_name in str_envs.items():
        val = os.getenv(env_key)
        if val and val.strip():
            env_overrides[attr_name] = val.strip()

    num_envs = {
        "ENERGY_THRESHOLD": ("energy_threshold", float),
        "LLM_COOLDOWN": ("llm_cooldown_seconds", int),
        "ALERT_COOLDOWN": ("alert_cooldown_seconds", int),
        "REPORT_INTERVAL": ("report_interval_seconds", int),
        "DEFAULT_NOTIONAL_VALUE": ("default_notional_value", float),
    }
    for env_key, (attr_name, cast) in num_envs.items():
        val = os.getenv(env_key)
        if val and val.strip():
            try:
                env_overrides[attr_name] = cast(val.strip())
            except (ValueError, TypeError):
                pass

    merged = {**env_overrides, **kwargs}
    return Settings(**merged)
