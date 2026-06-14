"""
配置中枢 (Config Hub)
=====================
集中管理系统所有配置项：API 密钥、交易参数、动能计算超参、LLM 风控参数。
所有敏感信息通过环境变量注入，严禁硬编码。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# 尝试从 .env 文件加载环境变量
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    """简易 .env 加载器，不依赖 python-dotenv 库。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AlpacaConfig:
    """Alpaca API 连接与认证配置 (Paper Trading 环境)。"""

    API_KEY: str = field(
        default_factory=lambda: os.getenv("ALPACA_API_KEY", "your_paper_api_key")
    )
    API_SECRET: str = field(
        default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", "your_paper_secret_key")
    )
    BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        )
    )
    DATA_FEED: str = field(
        default_factory=lambda: os.getenv("ALPACA_DATA_FEED", "iex")
    )  # 'iex' 免费, 'sip' 付费


@dataclass(frozen=True)
class StrategyConfig:
    """微观动能追踪策略的超参数配置。"""

    # ------------------------------------------------------------------
    # 目标标的池（高流动性科技股）
    # ------------------------------------------------------------------
    TICKERS: List[str] = field(default_factory=lambda: [
        "TSLA",   # 特斯拉
        "NVDA",   # 英伟达
        "AAPL",   # 苹果
    ])

    # ------------------------------------------------------------------
    # 动能计算参数
    # ------------------------------------------------------------------
    EMA_ALPHA: float = 0.15
    """速度 (Velocity) 的指数移动平均平滑系数。
       alpha 越大，对近期价格变动越敏感；alpha 越小，曲线越平滑。"""

    ENERGY_THRESHOLD: float = 50_000.0
    """能量告警阈值。当瞬时 Energy = |Velocity| × Volume 超过此值，
       系统将唤醒 LLM 进行风控裁决。单位：美元·股/秒。"""

    WINDOW_SECONDS: int = 300
    """5 分钟统计窗口（秒），用于计算窗口期内的能量积分。"""

    DEQUE_MAXLEN: int = 500
    """环形缓冲区最大长度，防止内存泄漏。"""

    # ------------------------------------------------------------------
    # 仓位与风控
    # ------------------------------------------------------------------
    MAX_POSITION_RATIO: float = 0.10
    """单次开仓最大资金占比（10%），即一次买入不超过总购买力的 10%。"""

    MIN_CONFIDENCE: float = 0.65
    """LLM 置信度最低阈值。低于此值的 BUY/SELL 信号将被丢弃。"""

    # ------------------------------------------------------------------
    # LLM 冷却与熔断
    # ------------------------------------------------------------------
    LLM_COOLDOWN_SECONDS: float = 30.0
    """LLM 冷却期：同一标的在冷却期内不会重复请求 LLM，防止过频调用。"""

    LLM_TIMEOUT_SECONDS: float = 5.0
    """LLM 请求超时（秒）。超时后直接熔断，放弃本次裁决。"""


@dataclass(frozen=True)
class LLMConfig:
    """大模型风控裁判的连接配置（兼容 OpenAI / OpenClaw 本地 Agent）。"""

    ENDPOINT: str = field(
        default_factory=lambda: os.getenv(
            "LLM_ENDPOINT",
            "https://api.openai.com/v1/chat/completions",
        )
    )
    API_KEY: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "your_llm_api_key")
    )
    MODEL: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini")
    )
    MAX_TOKENS: int = 300
    TEMPERATURE: float = 0.1  # 低温度保证输出结构稳定


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
alpaca_cfg = AlpacaConfig()
strategy_cfg = StrategyConfig()
llm_cfg = LLMConfig()
