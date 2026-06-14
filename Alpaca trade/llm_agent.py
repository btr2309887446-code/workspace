"""
大模型风控裁判 (LLM Agent)
==========================
在瞬时能量突破阈值时被异步唤醒，通过 aiohttp 将高频量价特征转化为结构化 Prompt，
交由 LLM 裁决是否执行交易。

特性：
- 纯异步 HTTP 调用（aiohttp）
- 5 秒超时熔断（Circuit Breaker）
- 同一标的冷却期（Cooldown），防止同一标的短时间内重复请求
- 极度容错的 JSON 解析器：剥离 Markdown 代码块，容忍字段缺失与类型问题
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

from config import llm_cfg, strategy_cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM 裁决结果
# ---------------------------------------------------------------------------
@dataclass
class LLMDecision:
    """LLM 风控裁判的结构化输出。"""
    action: str          # "BUY", "SELL", "HOLD"
    confidence: float    # 0.0 ~ 1.0
    reasoning: str       # 裁决理由


# ---------------------------------------------------------------------------
# 容错 JSON 解析器
# ---------------------------------------------------------------------------
def _robust_json_parse(text: str) -> Dict[str, Any]:
    """
    极度容错的 JSON 解析器。
    处理以下 LLM 常见输出畸形：
      - 被 ```json ... ``` 包裹
      - 被 ``` ... ``` 包裹（无语言标识）
      - 字段缺失或多余
      - 数字以字符串形式出现
      - 回复中夹杂多余解释文本

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的字典，保证包含 action, confidence, reasoning（缺失时用默认值填充）。
    """
    # 1. 尝试提取 Markdown 代码块内的 JSON
    code_block_patterns = [
        r"```json\s*([\s\S]*?)\s*```",   # ```json ... ```
        r"```\s*([\s\S]*?)\s*```",       # ``` ... ```
    ]
    for pattern in code_block_patterns:
        match = re.search(pattern, text)
        if match:
            text = match.group(1)
            break

    # 2. 尝试找到第一个 { 和最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    # 3. 解析 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 二次容错：用正则逐字段提取
        data = _fallback_field_extraction(text)
        if not data:
            logger.warning("JSON 解析完全失败，原始响应:\n%s", text[:500])
            return {"action": "HOLD", "confidence": 0.0, "reasoning": "JSON parse failed"}

    # 4. 标准化字段
    result: Dict[str, Any] = {
        "action": "HOLD",
        "confidence": 0.0,
        "reasoning": "",
    }

    raw_action = str(data.get("action", data.get("decision", "HOLD"))).strip().upper()
    if raw_action in ("BUY", "SELL", "HOLD"):
        result["action"] = raw_action
    else:
        result["action"] = "HOLD"

    try:
        result["confidence"] = float(data.get("confidence", 0.0))
    except (ValueError, TypeError):
        result["confidence"] = 0.0

    result["reasoning"] = str(data.get("reasoning", data.get("reason", "")))

    return result


def _fallback_field_extraction(text: str) -> Optional[Dict[str, Any]]:
    """正则逐字段提取作为 JSON 解析失败的兜底方案。"""
    result: Dict[str, Any] = {}
    patterns = {
        "action": r'"(?:action|decision)"\s*:\s*"(BUY|SELL|HOLD)"',
        "confidence": r'"confidence"\s*:\s*(0?\.\d+|1\.0|1|0)',
        "reasoning": r'"(?:reasoning|reason)"\s*:\s*"([^"]*)"',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result[key] = match.group(1)
    return result if result else None


# ---------------------------------------------------------------------------
# LLMAgent
# ---------------------------------------------------------------------------
class LLMAgent:
    """
    异步大模型风控裁判。
    被 analytics 的异动信号唤醒后，以结构化 Prompt 描述当前盘口微观状态，
    等待 LLM 返回 BUY / SELL / HOLD 决策。
    """

    def __init__(self):
        self._endpoint = llm_cfg.ENDPOINT
        self._api_key = llm_cfg.API_KEY
        self._model = llm_cfg.MODEL
        self._max_tokens = llm_cfg.MAX_TOKENS
        self._temperature = llm_cfg.TEMPERATURE
        self._timeout = strategy_cfg.LLM_TIMEOUT_SECONDS
        self._cooldown_sec = strategy_cfg.LLM_COOLDOWN_SECONDS
        self._min_confidence = strategy_cfg.MIN_CONFIDENCE

        # 冷却期记录：{symbol: last_call_timestamp}
        self._cooldowns: Dict[str, float] = {}
        self._cooldown_lock = asyncio.Lock()

        # 持久化的 aiohttp 会话（复用连接池）
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # 冷却期检查
    # ------------------------------------------------------------------
    async def _check_cooldown(self, symbol: str) -> bool:
        """
        检查指定标的是否仍在冷却期内。
        返回 True 表示可以调用（已过冷却期），False 表示仍需等待。
        """
        async with self._cooldown_lock:
            last = self._cooldowns.get(symbol, 0.0)
            if time.time() - last < self._cooldown_sec:
                return False
            self._cooldowns[symbol] = time.time()
            return True

    # ------------------------------------------------------------------
    # 构建 Prompt
    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(symbol: str, price: float, velocity: float, energy: float,
                      window_integral: float, avg_velocity: float) -> str:
        """
        将高频量价特征编码为标准化的 LLM Prompt。
        核心设计：用结构化数据而非自然语言长篇大论，提升 LLM 输出稳定性。
        """
        velocity_direction = "↑ 上涨动能" if velocity > 0 else "↓ 下跌动能"
        prompt = f"""You are a quantitative risk-control judge for high-frequency momentum trading.

Market Microstructure Data:
- Symbol: {symbol}
- Latest Price: ${price:.2f}
- Instantaneous Velocity (EMA of dP/dt): {velocity:+.4f} ({velocity_direction})
- Instantaneous Energy (|Velocity| × Volume): {energy:.2f}
- 5-Minute Window Energy Integral: {window_integral:.2f}
- 5-Minute Average Velocity: {avg_velocity:+.4f}

Task: Based on the above momentum signal, decide whether to BUY, SELL, or HOLD.
Rules:
1. BUY only when energy is high AND velocity is strongly positive (sustained upward momentum).
2. SELL only when velocity is strongly negative with high energy (panic selling signal — only if we hold this position).
3. HOLD when the signal is ambiguous or too weak.
4. Consider the 5-minute window stats to avoid reacting to isolated spikes.

Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{"action": "BUY", "confidence": 0.85, "reasoning": "Brief reason for the decision"}}"""
        return prompt

    # ------------------------------------------------------------------
    # 核心裁决方法
    # ------------------------------------------------------------------
    async def judge(self, symbol: str, price: float, velocity: float,
                    energy: float, window_integral: float, avg_velocity: float) -> Optional[LLMDecision]:
        """
        异步请求 LLM 进行风控裁决。

        Args:
            symbol: 股票代码。
            price: 触发异动时的价格。
            velocity: 平滑后的速度。
            energy: 瞬时能量。
            window_integral: 5 分钟窗口能量积分。
            avg_velocity: 5 分钟平均速度。

        Returns:
            LLMDecision 或 None（超时/熔断/解析失败）。
        """
        # 冷却期检查
        if not await self._check_cooldown(symbol):
            logger.debug("[%s] 仍在 LLM 冷却期内，跳过裁决", symbol)
            return None

        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )

        prompt = self._build_prompt(
            symbol, price, velocity, energy, window_integral, avg_velocity
        )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "You are a quantitative trading risk-control AI. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }

        try:
            async with self._session.post(
                self._endpoint, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        "LLM API 返回非 200 状态码: %d, body=%s", resp.status, body[:500]
                    )
                    return None

                data = await resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )

                if not content:
                    logger.warning("LLM 返回空内容")
                    return None

                parsed = _robust_json_parse(content)
                decision = LLMDecision(
                    action=parsed["action"],
                    confidence=parsed["confidence"],
                    reasoning=parsed["reasoning"],
                )

                # 置信度过滤
                if decision.confidence < self._min_confidence:
                    logger.info(
                        "[%s] LLM 判定 %s 但置信度 %.2f 低于阈值 %.2f，丢弃",
                        symbol, decision.action, decision.confidence, self._min_confidence,
                    )
                    return None

                logger.info(
                    "[%s] ✅ LLM 裁决: %s | 置信度: %.2f | 理由: %s",
                    symbol, decision.action, decision.confidence, decision.reasoning,
                )
                return decision

        except asyncio.TimeoutError:
            logger.error("[%s] LLM 请求超时 (%.1fs)，熔断", symbol, self._timeout)
            return None
        except aiohttp.ClientError:
            logger.exception("[%s] LLM 网络请求异常", symbol)
            return None
        except Exception:
            logger.exception("[%s] LLM 裁决过程发生未知异常", symbol)
            return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """关闭 aiohttp 会话，释放连接池资源。"""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("LLM Agent aiohttp 会话已关闭")
