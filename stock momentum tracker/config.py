"""
配置模块 (config.py)
=====================
职责：
  1. 管理日间/夜间多标的代码池（KRX 韩股 + US 美股）
  2. 配置传统金融数据源（Alpaca Markets）与 LLM 大模型（OpenAI 兼容）的 API 密钥及端点
  3. 设定“能量爆发阈值”，触发 LLM 风控大脑
  4. 配置工业级日志系统（控制台彩色 + 轮转日志文件）

所有敏感信息支持从环境变量或 .env 文件读取，绝不允许硬编码密钥。
"""

import os
import sys
import ctypes
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, ClassVar, Dict, List


# ============================================================================
# Windows 控制台 ANSI 颜色支持
# ============================================================================

def _enable_windows_ansi() -> None:
    """为 Windows 控制台启用 ANSI 转义序列支持。"""
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


_enable_windows_ansi()


# ============================================================================
# ANSI 颜色常量
# ============================================================================

class Ansi:
    """ANSI 转义序列常量，用于控制台彩色输出。"""
    RESET: ClassVar[str] = "\033[0m"
    BOLD: ClassVar[str] = "\033[1m"
    RED: ClassVar[str] = "\033[91m"
    GREEN: ClassVar[str] = "\033[92m"
    YELLOW: ClassVar[str] = "\033[93m"
    BLUE: ClassVar[str] = "\033[94m"
    MAGENTA: ClassVar[str] = "\033[95m"
    CYAN: ClassVar[str] = "\033[96m"
    WHITE: ClassVar[str] = "\033[97m"
    BOLD_RED: ClassVar[str] = "\033[1;91m"
    BOLD_YELLOW: ClassVar[str] = "\033[1;93m"
    BOLD_GREEN: ClassVar[str] = "\033[1;92m"
    BOLD_CYAN: ClassVar[str] = "\033[1;96m"


# ============================================================================
# 按级别着色的日志格式化器
# ============================================================================

class ColoredConsoleFormatter(logging.Formatter):
    """控制台日志按级别自动着色。"""
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
    全球半导体及科技巨头动量监控系统配置。

    所有参数均可通过环境变量或 get_settings() 关键字参数覆盖。
    """

    # ---- 日间标的池：韩股 ----
    krx_symbols: List[str] = field(default_factory=lambda: [
        "005930.KS",   # 三星电子
        "000660.KS",   # SK 海力士
    ])
    """韩股标的（Yahoo Finance 格式：代码.KS）"""

    # ---- 夜间标的池：美股 ----
    us_symbols: List[str] = field(default_factory=lambda: [
        "MU",           # 美光科技
        "WDC",          # 西部数据
        "NVDA",         # 英伟达
        "MRVL",         # Marvell Technology
        "AAPL",         # Apple
    ])
    """美股标的（标准股票代码）"""

    # ---- Alpaca Markets API（美股实时数据，可选） ----
    alpaca_api_key: str = ""
    """Alpaca API Key ID（从 https://alpaca.markets 获取）"""

    alpaca_api_secret: str = ""
    """Alpaca API Secret Key"""

    alpaca_use_paper: bool = True
    """True=使用 Paper Trading 数据源，False=使用 Live 数据源"""

    @property
    def alpaca_configured(self) -> bool:
        """Alpaca API 是否已配置"""
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    # ---- Yahoo Finance 备选数据源（无 API Key 要求） ----
    yf_poll_interval: float = 3.0
    """Yahoo Finance REST 轮询间隔（秒），建议 2~5s"""

    # ---- LLM 大模型配置（OpenAI 兼容接口） ----
    llm_api_endpoint: str = "https://api.openai.com/v1/chat/completions"
    """LLM API 端点（兼容 OpenAI Chat Completions 格式）"""

    llm_api_key: str = ""
    """LLM API 密钥"""

    llm_model: str = "gpt-4o-mini"
    """LLM 模型名称"""

    llm_timeout: float = 10.0
    """LLM 请求超时时间（秒），超时后自动熔断"""

    llm_max_tokens: int = 300
    """LLM 最大输出 Token 数"""

    llm_temperature: float = 0.2
    """LLM 温度参数（越低越确定）"""

    llm_cooldown_seconds: int = 60
    """同一标的 LLM 分析冷却期（秒），防止连续触发刷屏"""

    @property
    def llm_configured(self) -> bool:
        """LLM API 是否已配置"""
        return bool(self.llm_api_endpoint and self.llm_api_key)

    # ---- 量化指标参数 ----
    velocity_ema_alpha: float = 0.15
    """速度 EMA 平滑系数 α ∈ (0, 1]，推荐 0.1~0.2"""

    energy_window: int = 20
    """能量指标统计窗口（采样点数）"""

    history_maxlen: int = 5000
    """环形缓冲区最大长度，覆盖 5 分钟以上数据"""

    gap_threshold_seconds: float = 300.0
    """
    跳空检测阈值（秒）。
    若两 Tick 间隔超过此值（如跨午休、跨日），
    视为新会话，不计算该跨度的速度（避免跳空污染 EMA）。
    """

    # ---- 能量告警阈值 ----
    energy_threshold: float = 500000.0
    """
    能量爆发阈值。
    当瞬时能量超过此值时触发 LLM 风控分析。
    需根据实际标的的量价关系调整。
    """

    alert_cooldown_seconds: int = 30
    """告警冷却期（秒），同标的连续告警间隔"""

    # ---- 5 分钟周期报告 ----
    report_interval_seconds: int = 300
    """周期报告间隔（秒），默认 300 = 5 分钟"""

    report_dir: str = "reports"
    """报告文件存放目录"""

    report_filename_template: str = "Semiconductor_Momentum_{timestamp}.txt"
    """报告文件名模板，{timestamp} → YYYYMMDD_HHMM"""

    # ---- 日志配置 ----
    log_dir: str = "logs"
    log_file: str = "stock_momentum.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 7
    log_level: str = "INFO"

    # ---- 运行控制 ----
    shutdown_timeout: float = 5.0
    stats_interval: float = 120.0
    """状态摘要打印间隔（秒）"""

    session_check_interval: float = 30.0
    """盘口状态机轮询间隔（秒）"""


# ============================================================================
# 日志系统
# ============================================================================

def setup_logging(settings: Settings) -> logging.Logger:
    """配置双通道日志系统（控制台着色 + 文件轮转）。"""
    logger = logging.getLogger("StockMomentum")
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
# 配置工厂函数
# ============================================================================

def get_settings(**kwargs) -> Settings:
    """
    创建配置实例，支持多层覆盖。

    优先级：默认值 < 环境变量 < .env 文件 < 关键字参数

    支持的环境变量：
      ALPACA_API_KEY / ALPACA_API_SECRET
      LLM_API_KEY / LLM_API_ENDPOINT / LLM_MODEL
      ENERGY_THRESHOLD / REPORT_INTERVAL
    """
    # 尝试加载 .env 文件
    try:
        from dotenv import load_dotenv
        _env_path = Path(__file__).parent / ".env"
        if _env_path.exists():
            load_dotenv(_env_path)
    except ImportError:
        pass

    env_overrides: dict = {}

    str_envs = {
        "ALPACA_API_KEY": "alpaca_api_key",
        "ALPACA_API_SECRET": "alpaca_api_secret",
        "LLM_API_KEY": "llm_api_key",
        "LLM_API_ENDPOINT": "llm_api_endpoint",
        "LLM_MODEL": "llm_model",
    }

    float_envs = {
        "ENERGY_THRESHOLD": "energy_threshold",
    }
    int_envs = {
        "REPORT_INTERVAL": "report_interval_seconds",
        "LLM_COOLDOWN": "llm_cooldown_seconds",
        "ALERT_COOLDOWN": "alert_cooldown_seconds",
    }

    for env_key, attr_name in str_envs.items():
        val = os.getenv(env_key)
        if val and val.strip():
            env_overrides[attr_name] = val.strip()

    for env_key, attr_name in float_envs.items():
        val = os.getenv(env_key)
        if val and val.strip():
            try:
                env_overrides[attr_name] = float(val.strip())
            except (ValueError, TypeError):
                pass

    for env_key, attr_name in int_envs.items():
        val = os.getenv(env_key)
        if val and val.strip():
            try:
                env_overrides[attr_name] = int(val.strip())
            except (ValueError, TypeError):
                pass

    merged = {**env_overrides, **kwargs}
    return Settings(**merged)
