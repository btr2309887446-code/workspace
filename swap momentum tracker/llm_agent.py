"""
LLM 风控与策略大脑模块 (llm_agent.py)
=======================================
职责：
  1. 事件驱动型风控裁判——当 analytics.py 探测到能量突破阈值时被异步唤醒
  2. 将高频量价特征组装为 LLM 提示词，判断异动是"真实资金主导"还是"低流动性假洗盘"
  3. 兼容 OpenAI 兼容格式（ChatGPT / OpenClaw 本地 Agent / DeepSeek 等）
     以及 Google Gemini API（AI Studio）两种后端
  4. 纯异步 aiohttp 架构，超时熔断（Circuit Breaker），绝不对阻塞主行情流
  5. 强健 JSON 解析器——正则剥离 markdown、校验必需字段、容错兜底
  6. 每标的冷却期，防止高频连续触发刷屏

API 兼容层说明：
  本模块根据 endpoint URL 自动识别后端类型：
    - 包含 "generativelanguage.googleapis.com" → Google Gemini API
    - 其余 → OpenAI Chat Completions 兼容格式（含本地 OpenClaw 等 Agent）
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any, Union

import aiohttp

# ── 日志 ──────────────────────────────────────────────────────────────
logger = logging.getLogger("SwapMomentum.LLMAgent")


# ============================================================================
# API 格式枚举
# ============================================================================

class _ApiFormat:
    """LLM API 后端类型常量"""
    OPENAI_COMPAT = "openai_compat"   # OpenAI / DeepSeek / OpenClaw 等
    GEMINI = "gemini"                 # Google Gemini (AI Studio)


# ============================================================================
# 风控裁判核心类
# ============================================================================

class MarketLLMAgent:
    """
    基于 LLM 的事件驱动型风控决策引擎。

    使用示例：
        agent = MarketLLMAgent(
            api_endpoint="https://api.openai.com/v1/chat/completions",
            api_key="sk-xxx",
            model="gpt-4o-mini",
        )
        result = await agent.analyze(
            ticker="TSLA-USDT-SWAP",
            current_price=248.50,
            velocity=12.3456,
            energy=98765.43,
            five_min_stats={...},
        )
    """

    # ── 极度克制的 System Prompt ────────────────────────────────────
    SYSTEM_PROMPT = (
        "你是一个加密货币衍生品量化风控裁判。"
        "你只接收量价数据，只输出纯 JSON。"
        "禁止输出 Markdown 代码块、禁止输出任何解释性文字或问候语。"
        "禁止输出换行符以外的任何非 JSON 字符。"
        "你的输出将以程序化方式解析，任何格式偏差都将导致交易决策失败。\n"
        "\n"
        "判断规则：\n"
        "1. 高成交量 + 高速度 & 方向一致 → BUY 或 SELL（跟随趋势）\n"
        "2. 低成交量 + 高速度 → HOLD（低流动性假洗盘，不可信）\n"
        "3. 能量（Energy = |Velocity| × Volume）与近期均值比较："
        "远高于均值且成交量配合 → 真实突破；量能背离 → HOLD\n"
        "4. reasoning 字段严格限制 50 字以内\n"
        "\n"
        "输出格式（仅此 JSON，无任何前后缀）：\n"
        '{"action":"BUY","confidence":0.85,"reasoning":"量价齐升且高于5分钟均能3倍，跟随底层趋势"}'
    )

    # ── OpenAI 兼容格式的 System Prompt（追加 role 字段约束）───────
    SYSTEM_MESSAGE = {
        "role": "system",
        "content": SYSTEM_PROMPT,
    }

    def __init__(
        self,
        api_endpoint: str = "https://api.openai.com/v1/chat/completions",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout: float = 10.0,
        max_tokens: int = 200,
        temperature: float = 0.1,
        cooldown_seconds: int = 60,
    ):
        """
        初始化 LLM 风控裁判。

        Args:
            api_endpoint:  API 端点 URL（OpenAI 兼容或 Gemini）
            api_key:       API 密钥
            model:         模型名称
            timeout:       请求超时熔断时间（秒），超时直接返回 None
            max_tokens:    最大输出 token 数
            temperature:   温度参数（越低越确定）
            cooldown_seconds: 同标的冷却期（秒）
        """
        self.endpoint = api_endpoint
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.cooldown = cooldown_seconds

        # 自动检测 API 格式
        self._api_format = self._detect_api_format(api_endpoint)

        # 冷却期追踪
        self._cooldown_until: Dict[str, float] = {}

        # 运行统计（线程安全由 asyncio 单线程模型保证）
        self._call_count: int = 0
        self._success_count: int = 0
        self._fail_count: int = 0
        self._timeout_count: int = 0
        self._skip_cooldown_count: int = 0
        self._parse_fail_count: int = 0

        logger.info(
            f"LLM Agent 初始化 | endpoint={api_endpoint[:50]}... | "
            f"model={model} | format={self._api_format} | timeout={timeout}s"
        )

    # ------------------------------------------------------------------
    # 公开方法：主分析入口
    # ------------------------------------------------------------------

    async def analyze(
        self,
        ticker: str,
        current_price: float,
        velocity: float,
        energy: float,
        five_min_stats: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        异步分析一次能量异动事件（非阻塞）。

        完整调用链：
          check_cooldown → build_prompt → call_llm_api → parse_llm_response

        Args:
            ticker:         合约代码，如 "TSLA-USDT-SWAP"
            current_price:  当前成交价（USDT 本位）
            velocity:       当前 EMA 平滑速度（USDT/s）
            energy:         当前瞬时能量
            five_min_stats: analytics.MarketDynamicsCalculator.get_window_stats()
                            的返回值，包含 5 分钟聚合统计。None 时仅使用实时数据。

        Returns:
            None  → 冷却期内 / 调用超时 / 解析失败 / API 未配置
            {
                "action":      "BUY" | "SELL" | "HOLD",
                "confidence":  0.0 ~ 1.0,
                "reasoning":   "中文判断逻辑（≤50字）",
                "ticker":      str,
                "raw_response": str,
                "price":       float,
                "velocity":    float,
                "energy":      float,
                "timestamp":   float,
            }
        """
        # 未配置 API Key → 静默跳过
        if not self.api_key or not self.endpoint:
            logger.debug("LLM API 未配置，跳过分析")
            return None

        # 冷却期检查
        now = time.time()
        if ticker in self._cooldown_until and now < self._cooldown_until[ticker]:
            self._skip_cooldown_count += 1
            remaining = int(self._cooldown_until[ticker] - now)
            logger.debug(f"{ticker} LLM 冷却中（剩余 {remaining}s），跳过")
            return None

        # 组装提示词
        user_prompt = self._build_prompt(
            ticker=ticker,
            current_price=current_price,
            velocity=velocity,
            energy=energy,
            five_min_stats=five_min_stats,
        )

        # 调用 LLM
        self._call_count += 1
        try:
            raw_response = await self._call_llm_api(user_prompt)
        except asyncio.TimeoutError:
            self._fail_count += 1
            self._timeout_count += 1
            logger.error(
                f"LLM 调用超时熔断 | ticker={ticker} | timeout={self.timeout}s"
            )
            return None
        except aiohttp.ClientError as e:
            self._fail_count += 1
            logger.error(f"LLM HTTP 异常 | ticker={ticker} | error={e}")
            return None
        except Exception:
            self._fail_count += 1
            logger.exception(f"LLM 调用未预期异常 | ticker={ticker}")
            return None

        if not raw_response:
            self._fail_count += 1
            logger.error(f"LLM 返回空响应 | ticker={ticker}")
            return None

        # 解析 JSON 决策
        decision = self._parse_llm_response(raw_response)
        if decision is None:
            self._fail_count += 1
            self._parse_fail_count += 1
            logger.error(
                f"LLM 响应解析失败 | ticker={ticker} | "
                f"raw(len={len(raw_response)})={raw_response[:200]}"
            )
            return None

        # 设置冷却期
        self._cooldown_until[ticker] = now + self.cooldown
        self._success_count += 1

        result = {
            **decision,
            "ticker": ticker,
            "timestamp": now,
            "raw_response": raw_response,
            "price": current_price,
            "velocity": velocity,
            "energy": energy,
        }

        logger.info(
            f"LLM 决策 | {ticker} | action={result['action']} | "
            f"conf={result['confidence']:.2f} | {result['reasoning']}"
        )
        return result

    # ------------------------------------------------------------------
    # 内部：API 格式检测
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_api_format(endpoint: str) -> str:
        """
        根据 endpoint URL 自动检测 API 后端格式。

        规则：
          - 包含 "generativelanguage.googleapis.com" → Gemini
          - 其余（含本地 OpenClaw、DeepSeek、OpenAI 等）→ OpenAI 兼容
        """
        if "generativelanguage.googleapis.com" in endpoint:
            return _ApiFormat.GEMINI
        return _ApiFormat.OPENAI_COMPAT

    # ------------------------------------------------------------------
    # 内部：提示词组装
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        ticker: str,
        current_price: float,
        velocity: float,
        energy: float,
        five_min_stats: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        将实时量价特征与 5 分钟聚合统计组装为 LLM 单次对话 Prompt。

        Args:
            ticker:         合约代码
            current_price:  当前价格
            velocity:       当前速度
            energy:         当前能量
            five_min_stats: 5 分钟聚合统计字典，结构参见
                            MarketDynamicsCalculator.get_window_stats()

        Returns:
            格式化的中文 User Prompt 字符串
        """
        # 方向判定
        if velocity > 0.001:
            direction = "快速拉升（多头主导）"
        elif velocity < -0.001:
            direction = "快速下跌（空头主导）"
        else:
            direction = "横盘震荡"

        lines = [
            f"合约代码: {ticker}",
            f"当前价格: {current_price:.6f} USDT",
            f"价格方向: {direction}",
            f"瞬时速度: {velocity:+.6f} USDT/s",
            f"瞬时能量: {energy:.6f}",
        ]

        # 如果有 5 分钟聚合数据，追加统计信息
        if five_min_stats and isinstance(five_min_stats, dict):
            lines.append("")
            lines.append("--- 近5分钟聚合统计 ---")
            lines.append(f"采样数量: {five_min_stats.get('sample_count', 0)}")
            lines.append(f"5分钟涨跌幅: {five_min_stats.get('price_change_pct', 0):+.4f}%")
            lines.append(f"平均速度: {five_min_stats.get('avg_velocity', 0):+.6f} USDT/s")
            lines.append(f"速度标准差: {five_min_stats.get('std_velocity', 0):.6f}")
            lines.append(f"平均能量: {five_min_stats.get('avg_energy', 0):.6f}")
            lines.append(f"能量积分(∫Edt): {five_min_stats.get('energy_integral', 0):.2f}")
            lines.append(f"最高能量: {five_min_stats.get('max_energy', 0):.6f}")
            lines.append(f"最低能量: {five_min_stats.get('min_energy', 0):.6f}")
            lines.append(f"趋势方向: {five_min_stats.get('direction', '未知')}")
            lines.append(f"买盘能量占比: {five_min_stats.get('bull_ratio', 50):.1f}%")
            lines.append(f"总成交额: {five_min_stats.get('total_volume', 0):.2f} USDT")
        else:
            lines.append("")
            lines.append("--- 近期统计 ---")
            lines.append("（无 5 分钟聚合数据，仅依据瞬时指标判断）")

        lines.append("")
        lines.append("请判断该异动是跟随底层股票的真实突破还是低流动性假洗盘。")
        lines.append("仅输出 JSON，禁止任何其他文本。")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部：API 调用（多后端路由）
    # ------------------------------------------------------------------

    async def _call_llm_api(self, user_prompt: str) -> Optional[str]:
        """
        根据 _api_format 路由到对应的 API 调用方法。

        Args:
            user_prompt: User 角色的提示词文本

        Returns:
            LLM 响应的原始文本，失败或超时返回 None
        """
        if self._api_format == _ApiFormat.GEMINI:
            return await self._call_gemini_api(user_prompt)
        else:
            return await self._call_openai_compat_api(user_prompt)

    # ---- OpenAI 兼容 API ----

    async def _call_openai_compat_api(self, user_prompt: str) -> Optional[str]:
        """
        调用 OpenAI Chat Completions 兼容 API。

        兼容所有遵循 `/v1/chat/completions` 协议的端点：
          - OpenAI ChatGPT / Azure OpenAI
          - DeepSeek / 通义千问 / 文心一言
          - 本地 OpenClaw Agent 框架
          - Ollama / vLLM / text-generation-webui
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = {
            "model": self.model,
            "messages": [
                self.SYSTEM_MESSAGE,
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
                            f"OpenAI API 返回非 200 | status={resp.status} | "
                            f"body={body[:300]}"
                        )
                        return None

                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    return content.strip() if content else None

        except asyncio.TimeoutError:
            raise  # 向上抛出，由 analyze() 统一处理
        except aiohttp.ClientError as e:
            logger.error(f"OpenAI API HTTP 错误: {e}")
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"OpenAI API 响应结构异常: {e}")
        except Exception:
            logger.exception("OpenAI API 未预期异常")
        return None

    # ---- Google Gemini API ----

    async def _call_gemini_api(self, user_prompt: str) -> Optional[str]:
        """
        调用 Google Gemini API（AI Studio）。

        Gemini API 的特殊之处：
          1. API Key 作为 URL 查询参数 ?key= 传递，而非 Authorization Header
          2. 请求体结构为 Gemini 专用格式（contents / parts）
          3. 响应结构为 candidates[0].content.parts[0].text
        """
        # 构建 Gemini 端点 URL（含 API Key）
        gemini_url = (
            f"{self.endpoint.rstrip('/')}"
            f"?key={self.api_key}"
        )

        # 组合 System Prompt + User Prompt
        # Gemini 不支持独立的 system role，将系统指令与用户消息合并
        combined_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": combined_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE",
                },
            ],
        }

        headers = {
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    gemini_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            f"Gemini API 返回非 200 | status={resp.status} | "
                            f"body={body[:300]}"
                        )
                        return None

                    data = await resp.json()
                    # Gemini 响应结构: candidates[0].content.parts[0].text
                    candidates = data.get("candidates", [])
                    if not candidates:
                        # 可能被安全过滤器拦截
                        block_reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
                        logger.error(
                            f"Gemini 无 candidates（可能被安全过滤）| "
                            f"blockReason={block_reason}"
                        )
                        return None

                    content_obj = candidates[0].get("content", {})
                    parts = content_obj.get("parts", [])
                    text = parts[0].get("text", "") if parts else ""

                    return text.strip() if text else None

        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientError as e:
            logger.error(f"Gemini API HTTP 错误: {e}")
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Gemini API 响应结构异常: {e}")
        except Exception:
            logger.exception("Gemini API 未预期异常")
        return None

    # ------------------------------------------------------------------
    # 内部：JSON 解析（极端容错）
    # ------------------------------------------------------------------

    def _parse_llm_response(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """
        从 LLM 返回的原始文本中提取、解析并校验 JSON 决策。

        容错策略（按优先级依次尝试）：
          1. 正则剥离 ```json ... ``` / ``` ... ``` 包裹
          2. 正则提取首个完整 { ... } JSON 对象
          3. 直接 json.loads 解析 + 字段校验
          4. 将单引号替换为双引号后重试
          5. 全场失败 → 返回 None

        Args:
            raw_text: LLM 返回的原始文本

        Returns:
            {"action": str, "confidence": float, "reasoning": str} 或 None
        """
        if not raw_text or not isinstance(raw_text, str):
            logger.debug(f"parse_llm_response: 输入为空或非字符串 type={type(raw_text)}")
            return None

        text = raw_text.strip()
        if not text:
            return None

        # ── 策略 1：剥离 Markdown 代码块 ──
        # 匹配 ```json ... ``` 或 ``` ... ```
        md_stripped = re.sub(
            r"^```(?:json)?\s*\n", "", text, flags=re.IGNORECASE
        )
        md_stripped = re.sub(
            r"\n```\s*$", "", md_stripped, flags=re.IGNORECASE
        )
        # 也处理内嵌的代码块
        md_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?```", md_stripped, re.DOTALL | re.IGNORECASE
        )
        if md_match:
            md_stripped = md_match.group(1).strip()

        # ── 策略 2：提取首个 { ... } JSON 对象 ──
        brace_match = re.search(r"\{[^{}]*\{.*\}[^{}]*\}|\{.*\}", md_stripped, re.DOTALL)
        if brace_match:
            extracted = brace_match.group(0)
        else:
            extracted = md_stripped

        # ── 策略 3：直接 json.loads ──
        decision = self._try_parse_json(extracted)
        if decision:
            return decision

        # ── 策略 4：单引号 → 双引号后重试 ──
        fixed = extracted.replace("'", '"')
        decision = self._try_parse_json(fixed)
        if decision:
            logger.debug("parse_llm_response: 通过单引号→双引号修复成功")
            return decision

        # ── 策略 5：尝试无引号 key 加引号 ──
        try:
            key_fixed = re.sub(r'([{,])\s*(\w+)\s*:', r'\1"\2":', extracted)
            decision = self._try_parse_json(key_fixed)
            if decision:
                logger.debug("parse_llm_response: 通过 key 加引号修复成功")
                return decision
        except Exception:
            pass

        # 全部失败
        logger.error(
            f"parse_llm_response: 所有解析策略均失败 | "
            f"original(len={len(raw_text)})={raw_text[:200]}"
        )
        return None

    def _try_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        尝试将文本解析为 JSON 并校验必需字段。

        支持英文 + 中文键名双通道回退：
          英文                       中文兜底
          "action"       ←  "动作"、"操作"、"决策"
          "confidence"   ←  "置信度"、"概率"、"把握"
          "reasoning"    ←  "理由"、"逻辑"、"分析"、"原因"

        Returns:
            校验通过的决策 dict，或 None
        """
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(parsed, dict):
            return None

        # ── 字段提取：英文键优先 → 中文键兜底 ──

        # action：英文 "action" → 中文 "动作"/"操作"/"决策"
        action = parsed.get("action")
        if action is None:
            for cn_key in ("动作", "操作", "决策"):
                action = parsed.get(cn_key)
                if action is not None:
                    logger.debug(f"parse_llm_response: action 从中文键 '{cn_key}' 提取")
                    break

        action = str(action or "").strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            logger.warning(
                f"parse_llm_response: 无效 action='{action}'，修正为 HOLD"
            )
            action = "HOLD"

        # confidence：英文 "confidence" → 中文 "置信度"/"概率"/"把握"
        raw_conf = parsed.get("confidence")
        if raw_conf is None:
            for cn_key in ("置信度", "概率", "把握"):
                raw_conf = parsed.get(cn_key)
                if raw_conf is not None:
                    logger.debug(f"parse_llm_response: confidence 从中文键 '{cn_key}' 提取")
                    break
        if raw_conf is None:
            raw_conf = 0.5

        try:
            confidence = float(raw_conf)
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5

        # reasoning：英文 "reasoning" → 中文 "理由"/"逻辑"/"分析"/"原因"
        reasoning = parsed.get("reasoning")
        if reasoning is None:
            for cn_key in ("理由", "逻辑", "分析", "原因"):
                reasoning = parsed.get(cn_key)
                if reasoning is not None:
                    logger.debug(f"parse_llm_response: reasoning 从中文键 '{cn_key}' 提取")
                    break

        if not isinstance(reasoning, str):
            reasoning = str(reasoning or "")
        reasoning = reasoning.strip()

        if not reasoning:
            reasoning = "未提供分析"

        # 限制 reasoning 长度
        if len(reasoning) > 80:
            reasoning = reasoning[:77] + "..."

        return {
            "action": action,
            "confidence": round(confidence, 4),
            "reasoning": reasoning,
        }

    # ------------------------------------------------------------------
    # 公开：运行统计
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """获取 LLM Agent 运行统计。"""
        return {
            "total_calls": self._call_count,
            "success": self._success_count,
            "failure": self._fail_count,
            "timeout": self._timeout_count,
            "parse_fail": self._parse_fail_count,
            "skipped_cooldown": self._skip_cooldown_count,
            "api_format": self._api_format,
            "cooldown_active": {
                sym: max(0, int(t - time.time()))
                for sym, t in self._cooldown_until.items()
                if time.time() < t
            },
        }

    @property
    def is_configured(self) -> bool:
        """API Key 和 Endpoint 是否已配置"""
        return bool(self.api_key and self.endpoint)


# ============================================================================
# Mock Test —— 本地模拟测试（无网络也能运行部分逻辑）
# ============================================================================

async def _mock_llm_server(host: str = "127.0.0.1", port: int = 19999):
    """
    启动一个极简的 HTTP 服务器，模拟 OpenAI 兼容 API。

    总是返回一段刻意带 markdown 包裹的 JSON，
    用于测试 _parse_llm_response 的剥离能力。
    """
    from aiohttp import web

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        messages = body.get("messages", [])
        user_msg = messages[-1]["content"] if messages else ""

        # 模拟：如果 velocity 为正，返回 BUY；为负返回 SELL；否则 HOLD
        if "+" in user_msg and "快速拉升" in user_msg:
            mock_json = '{"action":"BUY","confidence":0.88,"reasoning":"量价共振且强于5分钟均值，多头真实"}'
        elif "-" in user_msg and "快速下跌" in user_msg:
            mock_json = '{"action":"SELL","confidence":0.72,"reasoning":"抛压放量，速度与能量均高于均值"}'
        else:
            mock_json = '{"action":"HOLD","confidence":0.45,"reasoning":"方向不明且量能不足"}'

        # 刻意用 markdown 包裹，测试解析器
        mock_content = f"```json\n{mock_json}\n```"

        return web.json_response({
            "choices": [
                {
                    "message": {"content": mock_content},
                    "finish_reason": "stop",
                }
            ]
        })

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Mock LLM 服务器已启动 → http://{host}:{port}/v1/chat/completions")
    return runner


async def _run_mock_test():
    """
    本地模拟测试——不依赖任何外部 API。

    流程：
      1. 启动本地 Mock LLM 服务器
      2. 创建 MarketLLMAgent 实例（指向 localhost）
      3. 传入 TSLA-USDT-SWAP 的虚拟能量爆发数据
      4. 执行 analyze() 并验证返回结果
      5. 测试冷却期机制
      6. 输出统计摘要
    """
    print("=" * 64)
    print("  MarketLLMAgent Mock Test")
    print("=" * 64)

    # 1. 启动 Mock 服务器
    runner = await _mock_llm_server()

    try:
        # 2. 实例化 Agent
        agent = MarketLLMAgent(
            api_endpoint="http://127.0.0.1:19999/v1/chat/completions",
            api_key="mock-key-not-needed",
            model="mock-model",
            timeout=5.0,
            cooldown_seconds=2,  # 短冷却期便于测试
        )

        print(f"\n  Agent created: format={agent._api_format}")

        # 3. 构造虚拟的 5 分钟聚合统计
        mock_five_min_stats = {
            "sample_count": 2847,
            "price_change_pct": 3.45,
            "avg_velocity": 2.3456,
            "std_velocity": 1.2345,
            "avg_energy": 12345.67,
            "energy_integral": 35123456.78,
            "max_energy": 98765.43,
            "min_energy": 234.56,
            "direction": "买盘主导 (偏多头)",
            "bull_ratio": 78.5,
            "total_volume": 56789012.34,
        }

        # ── Test A: 多头爆发 ──
        print("\n  [Test A] 多头能量爆发（velocity > 0）...")
        result_a = await agent.analyze(
            ticker="TSLA-USDT-SWAP",
            current_price=248.50,
            velocity=12.3456,
            energy=98765.43,
            five_min_stats=mock_five_min_stats,
        )

        assert result_a is not None, "Test A failed: analyze() returned None"
        assert result_a["action"] in ("BUY", "SELL", "HOLD")
        assert 0.0 <= result_a["confidence"] <= 1.0
        assert isinstance(result_a["reasoning"], str)
        assert result_a["ticker"] == "TSLA-USDT-SWAP"
        print(f"    Result: action={result_a['action']}, conf={result_a['confidence']:.2f}")
        print(f"    Reasoning: {result_a['reasoning']}")
        print("    [PASS]")

        # ── Test B: 冷却期 ──
        print("\n  [Test B] 冷却期测试（2 秒内再次调用）...")
        result_b = await agent.analyze(
            ticker="TSLA-USDT-SWAP",
            current_price=249.00,
            velocity=15.0,
            energy=120000.0,
        )
        assert result_b is None, "Test B failed: should be in cooldown"
        print("    [PASS] Cooldown working (returned None)")

        # ── Test C: 等待冷却期后再次调用 ──
        print("\n  [Test C] 冷却期结束后再次调用...")
        await asyncio.sleep(2.5)
        result_c = await agent.analyze(
            ticker="TSLA-USDT-SWAP",
            current_price=250.00,
            velocity=15.0,
            energy=150000.0,
            five_min_stats=mock_five_min_stats,
        )
        assert result_c is not None, "Test C failed: should fire after cooldown"
        print(f"    Result: action={result_c['action']}, conf={result_c['confidence']:.2f}")
        print("    [PASS]")

        # ── Test D: 空头爆发 ──
        print("\n  [Test D] 空头能量爆发（velocity < 0）...")
        # 需要等冷却期
        await asyncio.sleep(2.5)
        result_d = await agent.analyze(
            ticker="NVDA-USDT-SWAP",
            current_price=980.20,
            velocity=-25.6789,
            energy=234567.89,
            five_min_stats={
                "sample_count": 3100,
                "price_change_pct": -2.10,
                "avg_velocity": -15.4321,
                "std_velocity": 8.7654,
                "avg_energy": 54321.09,
                "energy_integral": 123456789.01,
                "max_energy": 234567.89,
                "min_energy": 1234.56,
                "direction": "卖盘主导 (偏空头)",
                "bull_ratio": 22.3,
                "total_volume": 98765432.10,
            },
        )
        assert result_d is not None, "Test D failed"
        print(f"    Result: action={result_d['action']}, conf={result_d['confidence']:.2f}")
        print(f"    Reasoning: {result_d['reasoning']}")
        print("    [PASS]")

        # ── Test E: JSON 解析器压力测试 ──
        print("\n  [Test E] JSON 解析器压力测试...")

        # E1: 正常 JSON
        r = agent._parse_llm_response('{"action":"BUY","confidence":0.9,"reasoning":"test"}')
        assert r and r["action"] == "BUY"
        print("    E1 正常 JSON: [PASS]")

        # E2: markdown 包裹
        r = agent._parse_llm_response('```json\n{"action":"SELL","confidence":0.7,"reasoning":"抛压"}\n```')
        assert r and r["action"] == "SELL"
        print("    E2 Markdown 包裹: [PASS]")

        # E3: 前后有文本
        r = agent._parse_llm_response('Based on data:\n{"action":"HOLD","confidence":0.5,"reasoning":"方向不明"}')
        assert r and r["action"] == "HOLD"
        print("    E3 前后有文本: [PASS]")

        # E4: 单引号
        r = agent._parse_llm_response("{'action':'BUY','confidence':0.8,'reasoning':'test'}")
        assert r and r["action"] == "BUY"
        print("    E4 单引号修复: [PASS]")

        # E5: 无效 action
        r = agent._parse_llm_response('{"action":"INVALID","confidence":0.5}')
        assert r and r["action"] == "HOLD"
        print("    E5 无效 action 修正: [PASS]")

        # E6: 缺失字段
        r = agent._parse_llm_response('{"action":"BUY"}')
        assert r and r["action"] == "BUY" and r["confidence"] == 0.5
        print("    E6 缺失字段兜底: [PASS]")

        # E7: 超界 confidence
        r = agent._parse_llm_response('{"action":"SELL","confidence":999}')
        assert r and r["confidence"] == 1.0
        print("    E7 超界 confidence 钳制: [PASS]")

        # E8: 纯垃圾
        r = agent._parse_llm_response("not valid json at all")
        assert r is None
        print("    E8 纯垃圾输入: [PASS]")

        # E9: 空字符串
        r = agent._parse_llm_response("")
        assert r is None
        print("    E9 空字符串: [PASS]")

        # E10: None
        r = agent._parse_llm_response(None)
        assert r is None
        print("    E10 None 输入: [PASS]")

        print("    [PASS] All 10 parser tests passed")

        # ── 统计输出 ──
        print("\n  ── Agent Stats ──")
        stats = agent.get_stats()
        for k, v in stats.items():
            print(f"    {k}: {v}")

        print("\n" + "=" * 64)
        print("  ALL MOCK TESTS PASSED")
        print("=" * 64)

    finally:
        await runner.cleanup()
        await asyncio.sleep(0.1)  # 等待清理完成


# ============================================================================
# __main__ 入口
# ============================================================================

if __name__ == "__main__":
    import sys

    # 若没有 argparse，直接运行 mock test
    if len(sys.argv) > 1 and sys.argv[1] == "--live":
        print(
            "Live mode requires LLM_API_KEY and LLM_API_ENDPOINT environment variables.\n"
            "Usage: python llm_agent.py          # Mock test (default)\n"
            "       python llm_agent.py --live   # Live API test (requires env vars)"
        )
    else:
        print("Running mock test (no external API needed)...\n")
        try:
            asyncio.run(_run_mock_test())
        except KeyboardInterrupt:
            print("\nInterrupted.")
