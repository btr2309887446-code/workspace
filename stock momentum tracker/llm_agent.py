"""
LLM 风控与策略大脑模块 (llm_agent.py)
=======================================
职责：
  1. 当 analytics.py 检测到能量突破阈值时，由 pipeline.py 异步唤醒
  2. 将高频量价特征（Ticker、价格、Velocity、Energy、近期统计）转化为 LLM 提示词
  3. 调用 OpenAI 兼容的 LLM API，判定异动是"真实突破"还是"假洗盘"
  4. 强约束输出为纯 JSON：{action, confidence, reasoning}
  5. 严格的超时熔断、JSON 解析容错、冷却期机制

安全特性：
  - 纯异步 aiohttp 请求，10 秒超时熔断
  - 同一标的 60 秒冷却期，防止连续触发
  - 强健 JSON 解析器——剥离 markdown 标记、补充缺失字段
  - 所有异常被 try-except 严密包裹，绝不阻塞主事件循环
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any

import aiohttp

from config import Settings, Ansi

logger = logging.getLogger("StockMomentum.LLMAgent")


# ============================================================================
# LLM 风控大脑
# ============================================================================

class MarketLLMAgent:
    """
    基于 LLM 的事件驱动型风控决策引擎。

    生命周期：
      1. pipeline.py 在能量阈值突破时调用 analyze()
      2. 检查冷却期 → 构建提示词 → 异步调用 LLM → 解析 JSON 响应
      3. 返回结构化决策 {action, confidence, reasoning}

    约束：
      - 同一标的在冷却期内不重复调用
      - LLM 调用超时或解析失败时返回 None，不影响主流程
    """

    # System Prompt —— 强制约束 LLM 的角色和输出格式
    SYSTEM_PROMPT = """你是一位世界顶级的量化交易分析师，专注于半导体和科技股。

你收到高频量价数据后，需要判断当前的异动属于：
- BUY：真实多头突破，动能强劲且有量支撑
- SELL：真实空头突破，抛压沉重且有量支撑
- HOLD：噪音/假突破/洗盘，不具备持续性

分析要点：
1. 速度（Velocity）与能量（Energy）是否同步——高速度+低能量=虚假波动
2. 近期趋势是否与当前方向一致——顺势突破更可信
3. 能量峰值是短暂脉冲还是持续高能

严格输出格式（仅输出 JSON，不要任何其他文本）：
```json
{"action": "BUY", "confidence": 0.85, "reasoning": "量价齐升，动能持续放大，突破前高可能性大"}
```
action 只能是 BUY/SELL/HOLD 之一。confidence 是 0.0-1.0 的浮点数。reasoning 用中文简要分析。"""

    def __init__(self, settings: Settings):
        """
        初始化 LLM 风控大脑。

        Args:
            settings: 系统配置实例
        """
        self.settings = settings
        self.endpoint: str = settings.llm_api_endpoint
        self.api_key: str = settings.llm_api_key
        self.model: str = settings.llm_model
        self.timeout: float = settings.llm_timeout
        self.max_tokens: int = settings.llm_max_tokens
        self.temperature: float = settings.llm_temperature
        self.cooldown: int = settings.llm_cooldown_seconds

        # 每标的冷却期追踪
        self._cooldown_until: Dict[str, float] = {}

        # 统计
        self._call_count: int = 0
        self._success_count: int = 0
        self._fail_count: int = 0
        self._skip_cooldown_count: int = 0

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def analyze(
        self,
        symbol: str,
        price: float,
        velocity: float,
        energy: float,
        recent_snapshot: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        异步调用 LLM 分析当前异动。

        完整的调用链：
          check_cooldown → build_prompt → call_llm → parse_response

        Args:
            symbol:          股票代码
            price:           当前价格
            velocity:        当前 EMA 速度
            energy:          当前能量
            recent_snapshot: MarketDynamicsCalculator.get_recent_snapshot() 的返回值

        Returns:
            None                  冷却期内 / 调用失败 / LLM 未配置
            {
                "action":     "BUY"/"SELL"/"HOLD",
                "confidence": 0.0-1.0,
                "reasoning":  "中文分析文本",
                "raw_response": "LLM 原始响应文本",
                "symbol":     str,
                "timestamp":  float,
            }
        """
        # 未配置 LLM 时静默跳过
        if not self.settings.llm_configured:
            logger.debug("LLM 未配置，跳过分析")
            return None

        # 冷却期检查
        now = time.time()
        if symbol in self._cooldown_until and now < self._cooldown_until[symbol]:
            self._skip_cooldown_count += 1
            logger.debug(f"{symbol} LLM 冷却中，跳过（剩余 {self._cooldown_until[symbol] - now:.0f}s）")
            return None

        # 构建提示词
        user_prompt = self._build_user_prompt(
            symbol, price, velocity, energy, recent_snapshot
        )

        # 调用 LLM
        self._call_count += 1
        try:
            raw_response = await self._call_llm_api(user_prompt)
        except asyncio.TimeoutError:
            self._fail_count += 1
            logger.error(f"LLM 调用超时（{self.timeout}s）| symbol={symbol}")
            return None
        except Exception:
            self._fail_count += 1
            logger.exception(f"LLM 调用异常 | symbol={symbol}")
            return None

        if not raw_response:
            self._fail_count += 1
            logger.error(f"LLM 返回空响应 | symbol={symbol}")
            return None

        # 解析 JSON
        decision = self._parse_response(raw_response)

        if decision is None:
            self._fail_count += 1
            logger.error(f"LLM 响应解析失败 | symbol={symbol} | raw={raw_response[:200]}")
            return None

        # 设置冷却期
        self._cooldown_until[symbol] = now + self.cooldown
        self._success_count += 1

        # 组装完整结果
        result = {
            **decision,
            "symbol": symbol,
            "timestamp": now,
            "raw_response": raw_response,
            "price": price,
            "velocity": velocity,
            "energy": energy,
        }

        logger.info(
            f"LLM 决策 | {symbol} | action={result['action']} | "
            f"confidence={result['confidence']:.2f} | {result['reasoning'][:60]}"
        )

        return result

    # ------------------------------------------------------------------
    # 内部：提示词构建
    # ------------------------------------------------------------------

    def _build_user_prompt(
        self,
        symbol: str,
        price: float,
        velocity: float,
        energy: float,
        recent: Dict[str, Any],
    ) -> str:
        """
        构建发送给 LLM 的 User Prompt。

        将结构化的量价数据转化为自然语言描述，
        包含当前值、近期趋势、统计摘要等。
        """
        # 方向
        if velocity > 0.001:
            direction = "快速拉升"
        elif velocity < -0.001:
            direction = "快速下跌"
        else:
            direction = "横盘"

        # 近期趋势
        recent_trend = recent.get("trend", "flat")
        if recent_trend == "up":
            trend_desc = "近期整体呈上升趋势"
        elif recent_trend == "down":
            trend_desc = "近期整体呈下跌趋势"
        else:
            trend_desc = "近期无明显趋势"

        # 构建统计描述
        v_mean = recent.get("velocity_mean", 0)
        e_mean = recent.get("energy_mean", 0)
        sample_count = recent.get("sample_count", 0)

        prompt = f"""请分析以下股票异动：

股票代码: {symbol}
当前价格: {price:.2f} USD
价格方向: {direction}

=== 实时动量指标 ===
瞬时速度: {velocity:+.6f} USD/s
瞬时能量: {energy:.2f}

=== 近期统计 (最近 {sample_count} 个采样点) ===
平均速度: {v_mean:+.6f} USD/s
平均能量: {e_mean:.2f}
近期趋势: {trend_desc}

请判断这是真实突破还是假洗盘，仅输出 JSON。"""

        return prompt

    # ------------------------------------------------------------------
    # 内部：LLM API 调用
    # ------------------------------------------------------------------

    async def _call_llm_api(self, user_prompt: str) -> Optional[str]:
        """
        通过 aiohttp 异步调用 OpenAI 兼容的 LLM API。

        Returns:
            LLM 响应的文本内容，失败返回 None
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            f"LLM API 返回非 200 | status={resp.status} | body={body[:200]}"
                        )
                        return None

                    data = await resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                    if not content:
                        logger.error("LLM 响应中无 content 字段")
                        return None

                    return content.strip()

        except asyncio.TimeoutError:
            raise  # 向上抛出，由 analyze() 捕获
        except aiohttp.ClientError as e:
            logger.error(f"LLM HTTP 请求失败: {e}")
        except Exception:
            logger.exception("LLM API 调用未预期异常")

        return None

    # ------------------------------------------------------------------
    # 内部：JSON 解析（强健容错）
    # ------------------------------------------------------------------

    def _parse_response(self, text: str) -> Optional[Dict[str, Any]]:
        """
        从 LLM 返回的原始文本中提取并校验 JSON 决策。

        容错处理：
          1. 剥离 ```json ... ``` markdown 代码块
          2. 剥离前导/后缀的非 JSON 文本
          3. 尝试多种 JSON 提取策略
          4. 校验必需字段（action, confidence, reasoning）
          5. 缺失字段填入默认值

        Args:
            text: LLM 返回的原始响应文本

        Returns:
            解析成功的 dict，失败返回 None
        """
        if not text:
            return None

        cleaned = text.strip()

        # 策略 1：提取 ```json ... ``` 代码块
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
        if json_match:
            cleaned = json_match.group(1).strip()

        # 策略 2：提取首个 { ... } JSON 对象
        brace_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace_match:
            cleaned = brace_match.group(0)

        # 策略 3：直接解析
        try:
            decision = json.loads(cleaned)
        except json.JSONDecodeError:
            # 策略 4：修复常见格式问题后重试
            try:
                cleaned = cleaned.replace("'", '"')  # 单引号 → 双引号
                cleaned = re.sub(r"(\w+):", r'"\1":', cleaned)  # 无引号 key → 加引号
                decision = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.error(f"JSON 解析最终失败 | raw={text[:300]}")
                return None

        if not isinstance(decision, dict):
            return None

        # 字段校验与规范化
        action = decision.get("action", "").strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            logger.warning(f"无效 action: {action}，修正为 HOLD")
            action = "HOLD"

        confidence = decision.get("confidence", 0.5)
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5

        reasoning = decision.get("reasoning", "未提供分析")
        if not isinstance(reasoning, str):
            reasoning = str(reasoning)

        return {
            "action": action,
            "confidence": round(confidence, 4),
            "reasoning": reasoning,
        }

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """获取 LLM 调用统计"""
        return {
            "total_calls": self._call_count,
            "success": self._success_count,
            "failure": self._fail_count,
            "skipped_cooldown": self._skip_cooldown_count,
            "cooldown_active": {
                sym: max(0, int(t - time.time()))
                for sym, t in self._cooldown_until.items()
                if time.time() < t
            },
        }
