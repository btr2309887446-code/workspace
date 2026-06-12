"""
================================================================================
Event-Driven Sentiment & Signal Aggregator
事件驱动型资产情绪分析与信号聚合系统
================================================================================

核心架构 (4 Tiers):
  1. Data Acquisition      — 宏观/行业事件驱动的广度新闻抓取
  2. LLM Analysis          — 大模型动态标的识别与情绪打分
  3. Signal Aggregation    — 数学平滑与交易信号生成
  4. Portfolio Allocation  — 资金分配与仓位优化

输出文件:
  - raw_llm_analysis.json           (LLM 原始推导逻辑)
  - daily_aggregated_signals.csv    (平滑后交易信号)
  - daily_execution_orders.csv      (可执行交易指令)

依赖安装:
  pip install duckduckgo-search openai
  (若启用Tavily: pip install requests)
================================================================================
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Optional

# =============================================================================
# ==================== 第三方库导入 ============================================
# =============================================================================
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]

# =============================================================================
# ========================= 配置区 (CONFIGURATION) =============================
# =============================================================================

# --- 搜索引擎配置 ---
USE_TAVILY = True                      # True = Tavily; False = DuckDuckGo (免费)
TAVILY_API_KEY = "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"

# --- LLM 配置 (OpenAI 兼容接口) ---
LLM_API_KEY = "sk-46ea60beccc44463ab9775d94e5fa883"
LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-pro"

# --- 采集与分析参数 ---
SEARCH_DAYS_BACK = 3                   # 搜索过去 N 天
MAX_RESULTS_PER_QUERY = 8             # 每条搜索词最多抓取条数
LLM_BATCH_SIZE = 5                     # LLM 每批分析 N 条新闻
LLM_MAX_WORKERS = 3                    # LLM 并发批次数
REQUEST_DELAY = 1.5                    # 搜索引擎请求间隔（秒）
LLM_RETRY_DELAY = 2.0                  # LLM 失败重试间隔（秒）
MAX_RETRIES = 3

# --- 资金分配与仓位参数 (Layer 4) ---
SIGNAL_THRESHOLD = 0.3                # 信号强度过滤阈值，abs(signal) < threshold → 放弃
INITIAL_CAPITAL = 1000.0              # 初始资金池（美元）

# --- 输出路径 ---
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentiment_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =============================================================================
# ======================== 资产池 (供 LLM 参考) ================================
# =============================================================================

ASSET_POOL: dict[str, dict[str, str]] = {
    "Tech_AI": {
        "NVDA": "NVIDIA",
        "AMD": "Advanced Micro Devices",
        "MSFT": "Microsoft",
    },
    "Storage": {
        "MU": "Micron Technology",
        "WDC": "Western Digital",
        "STX": "Seagate Technology",
    },
    "Hedge": {
        "GLD": "Gold market or SPDR Gold Trust",
        "USO": "Crude Oil market or WTI/Brent",
    },
}

# =============================================================================
# ================== 广度搜索词 (EVENT-DRIVEN BROAD QUERIES) ====================
# =============================================================================

BROAD_SEARCH_QUERIES: dict[str, list[str]] = {
    "Tech_AI_Compute": [
        "tech industry breaking news catalysts",
        "AI semiconductor supply chain news",
        "artificial intelligence data center CapEx",
    ],
    "Storage_Hardware": [
        "memory market NAND DRAM pricing news",
        "hardware storage inventory trends",
        "semiconductor memory supply demand",
    ],
    "Macro_Hedge": [
        "macroeconomy Fed rate inflation news",
        "gold crude oil commodity market breaking",
        "geopolitical oil supply disruption macro",
    ],
}


def _build_asset_pool_text() -> str:
    """生成供 LLM 识别标的的资产池描述文本。"""
    lines = ["=== KNOWN ASSET UNIVERSE ==="]
    for category, assets in ASSET_POOL.items():
        lines.append(f"{category}:")
        for ticker, name in assets.items():
            lines.append(f"  {ticker} = {name}")
    return "\n".join(lines)


# =============================================================================
# ======================== LLM SYSTEM PROMPT (NEW) =============================
# =============================================================================

SYSTEM_PROMPT = r"""
You are an elite Event-Driven quantitative strategy researcher covering US equities
and macro commodities. Your task: read incoming news streams, filter out noise
(articles with zero material market impact), and dynamically identify specific
affected assets.

=== RULES ===

1. DYNAMIC TICKER IDENTIFICATION:
   - Autonomously find stocks or commodity tickers directly impacted by the news.
   - Examples: SSD price surge -> MU, WDC; geopolitical escalation -> USO, GLD.
   - You MAY identify tickers beyond the known universe if justified (e.g., SMH, SOXX).

2. CROSS-HEDGING LOGIC:
   - The same news can be Bearish for Tech stocks AND Bullish for Hedge assets.
   - Example: Middle East conflict escalation -> Bearish for NVDA/AMD (risk-off,
     supply chain risk) BUT Bullish for USO (supply disruption premium) and
     GLD (safe-haven demand).
   - Hawkish Fed / strong USD -> Bearish for GLD, potentially Bearish for growth/tech.
   - Dovish Fed / rate cuts -> Bullish for all risk assets + gold.

3. SENTIMENT & CONFIDENCE:
   - direction: "Bullish" | "Bearish"
   - confidence_score: 1-10 integer (1 = mild whisper, 10 = paradigm-shifting event)

4. CATALYST MECHANISM:
   - Write a concise chain-of-logic (≤50 words) explaining WHY the news drives
     the designated direction for that specific ticker.

=== OUTPUT FORMAT (STRICT JSON ONLY, NO MARKDOWN FENCES) ===

{
  "has_market_moving_news": true,
  "triggered_signals": [
    {
      "news_headline": "core headline of the news",
      "market_sector": "Tech | Storage | Macro_Hedge | Others",
      "impacted_assets": [
        {
          "ticker": "MU",
          "direction": "Bullish",
          "confidence_score": 8,
          "catalyst_mechanism": "Micron's NAND ASP guidance raise signals tightening supply, boosting margin outlook."
        }
      ]
    }
  ]
}

If absolutely no material news exists, output:
{"has_market_moving_news": false, "triggered_signals": []}
"""

# =============================================================================
# ======================== 去重 & 日期工具 =====================================
# =============================================================================


def _news_cache_key(title: str, url: str) -> str:
    raw = f"{title}||{url}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_within_days(date_str: str, days_back: int) -> bool:
    if not date_str:
        return True
    cutoff = datetime.now() - timedelta(days=days_back + 1)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt >= cutoff
        except ValueError:
            continue
    match = re.search(r"(\d+)\s+(hour|day|minute)s?\s+ago", date_str, re.I)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit in ("hour", "minute"):
            return True
        if unit == "day":
            return value <= days_back
    return True


def _clean_json_response(raw: str) -> str:
    """从 LLM 回复中提取纯净 JSON。"""
    # 去除 ```json ... ``` / ``` ... ``` 包裹
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if match:
        raw = match.group(1)
    raw = raw.strip().lstrip("\ufeff")
    # 取第一个 { 到最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    return raw


# =============================================================================
# ==================== LAYER 1: NEWS DATA FETCHER ==============================
# =============================================================================


class NewsDataFetcher:
    """
    事件驱动型广度新闻抓取器。
    针对预定义的宏观/行业方向进行搜索，不再按个股轮询。
    输出: 去重、日期过滤后的扁平新闻流 (News Stream)。
    """

    def __init__(
        self,
        days_back: int = SEARCH_DAYS_BACK,
        max_per_query: int = MAX_RESULTS_PER_QUERY,
        delay: float = REQUEST_DELAY,
    ):
        self.days_back = days_back
        self.max_per_query = max_per_query
        self.delay = delay
        self._seen_keys: set[str] = set()

    # ------------------------------------------------------------------
    # DDG 引擎
    # ------------------------------------------------------------------
    def _fetch_ddg(self, query: str) -> list[dict[str, Any]]:
        if DDGS is None:
            raise RuntimeError("duckduckgo-search 未安装: pip install duckduckgo-search")

        results: list[dict[str, Any]] = []
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.news(query, region="wt-wt", safesearch="off",
                                     max_results=self.max_per_query * 2))
        except Exception:
            print(f"  [DDG-ERR] {query[:60]}...")
            return results

        for item in raw:
            title = item.get("title", "")
            url = item.get("url", "")
            key = _news_cache_key(title, url)
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)

            date_str = item.get("date", "")
            if not _is_within_days(date_str, self.days_back):
                continue

            results.append({
                "title": title,
                "url": url,
                "snippet": item.get("body", ""),
                "date": date_str,
                "source_query": query,
            })
            if len(results) >= self.max_per_query:
                break
        return results

    # ------------------------------------------------------------------
    # Tavily 引擎
    # ------------------------------------------------------------------
    def _fetch_tavily(self, query: str) -> list[dict[str, Any]]:
        import requests

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "advanced",
            "include_answer": False,
            "max_results": self.max_per_query,
            "days": self.days_back,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            print(f"  [TAVILY-ERR] {query[:60]}...")
            return []

        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            key = _news_cache_key(title, url)
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)

            results.append({
                "title": title,
                "url": url,
                "snippet": item.get("content", ""),
                "date": item.get("published_date", ""),
                "source_query": query,
            })
        return results[: self.max_per_query]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def build_news_stream(self) -> list[dict[str, Any]]:
        """
        遍历 BROAD_SEARCH_QUERIES，逐个搜索、去重、过滤，
        返回扁平化新闻流列表。
        """
        all_news: list[dict[str, Any]] = []
        seen_in_stream: set[str] = set()

        total_queries = sum(len(v) for v in BROAD_SEARCH_QUERIES.values())
        q_index = 0

        for sector, queries in BROAD_SEARCH_QUERIES.items():
            for query in queries:
                q_index += 1
                print(f"  [{q_index}/{total_queries}] [{sector}] \"{query}\"")

                try:
                    if USE_TAVILY:
                        batch = self._fetch_tavily(query)
                    else:
                        batch = self._fetch_ddg(query)
                except Exception as e:
                    print(f"    [ERROR] {e}")
                    batch = []

                added = 0
                for item in batch:
                    key = _news_cache_key(item["title"], item["url"])
                    if key in seen_in_stream:
                        continue
                    seen_in_stream.add(key)
                    item["source_sector"] = sector
                    all_news.append(item)
                    added += 1

                print(f"    -> {added} unique articles added")
                time.sleep(self.delay)

        print(f"\n  [NewsStream] Total unique articles: {len(all_news)}")
        return all_news


# =============================================================================
# ==================== LAYER 2: LLM SENTIMENT ANALYZER =========================
# =============================================================================


class LLMSentimentAnalyzer:
    """
    LLM 新闻情绪分析器。
    将新闻流分批（每批 LLM_BATCH_SIZE 条）喂给大模型，动态识别标的并评分。
    """

    def __init__(self):
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            if OpenAI is None:
                raise RuntimeError("openai 未安装: pip install openai")
            self._client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        return self._client

    # ------------------------------------------------------------------
    # 构建批次 prompt
    # ------------------------------------------------------------------
    def _build_batch_user_prompt(self, news_batch: list[dict[str, Any]]) -> str:
        asset_text = _build_asset_pool_text()

        articles = []
        for i, item in enumerate(news_batch):
            articles.append(
                f"=== ARTICLE #{i + 1} ===\n"
                f"TITLE: {item.get('title', 'N/A')}\n"
                f"DATE: {item.get('date', 'N/A')}\n"
                f"SNIPPET: {item.get('snippet', 'N/A')}"
            )

        return (
            f"{asset_text}\n\n"
            f"You are analyzing {len(news_batch)} news articles. "
            f"For EACH article, if it has material market impact, output "
            f"a triggered_signal entry. Output ONLY valid JSON.\n\n"
            + "\n\n".join(articles)
        )

    # ------------------------------------------------------------------
    # 单批调用（含重试）
    # ------------------------------------------------------------------
    def _call_llm_batch(
        self, news_batch: list[dict[str, Any]], batch_idx: int, total_batches: int
    ) -> dict[str, Any]:
        user_prompt = self._build_batch_user_prompt(news_batch)

        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                    max_tokens=3000,
                )
                raw = response.choices[0].message.content or ""
                cleaned = _clean_json_response(raw)
                parsed = json.loads(cleaned)

                if not isinstance(parsed.get("triggered_signals"), list):
                    raise ValueError("triggered_signals must be a list")

                # 附上原始新闻引用
                parsed["_batch_meta"] = {
                    "batch_idx": batch_idx,
                    "total_batches": total_batches,
                    "articles_in_batch": [
                        {"title": n["title"], "url": n["url"], "date": n.get("date", "")}
                        for n in news_batch
                    ],
                }
                return parsed

            except json.JSONDecodeError:
                print(f"  [RETRY] Batch {batch_idx}: JSON parse failed (attempt {attempt + 1})")
                time.sleep(LLM_RETRY_DELAY)
            except Exception as e:
                print(f"  [RETRY] Batch {batch_idx}: {e} (attempt {attempt + 1})")
                time.sleep(LLM_RETRY_DELAY)

        # 全部重试失败
        return {
            "has_market_moving_news": False,
            "triggered_signals": [],
            "_error": f"Batch {batch_idx} failed after {MAX_RETRIES} retries",
        }

    # ------------------------------------------------------------------
    # 主入口：并发分析
    # ------------------------------------------------------------------
    def analyze_stream(self, news_stream: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        分批并发调用 LLM，返回所有批次的解析结果列表。
        每批 batch_size 条新闻。
        """
        if not news_stream:
            print("[LLM] News stream is empty, skipping analysis.")
            return []

        # 分批
        batches = []
        for i in range(0, len(news_stream), LLM_BATCH_SIZE):
            batches.append(news_stream[i : i + LLM_BATCH_SIZE])

        total = len(batches)
        print(f"\n{'=' * 60}")
        print(f"  LLM Analysis: {len(news_stream)} articles → {total} batches "
              f"(batch_size={LLM_BATCH_SIZE})")
        print(f"{'=' * 60}\n")

        results: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as executor:
            future_map = {}
            for idx, batch in enumerate(batches):
                future = executor.submit(self._call_llm_batch, batch, idx + 1, total)
                future_map[future] = idx

            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    parsed = future.result()
                except Exception as e:
                    parsed = {
                        "has_market_moving_news": False,
                        "triggered_signals": [],
                        "_error": str(e),
                    }
                results.append(parsed)
                print(f"  [Batch {idx + 1}/{total}] done "
                      f"({len(parsed.get('triggered_signals', []))} signals)")

        return results


# =============================================================================
# ==================== LAYER 3: SIGNAL AGGREGATOR ==============================
# =============================================================================


class SignalAggregator:
    """
    信号聚合与平滑器。
    从 LLM 的多批次解析结果中提取所有信号，按 Ticker 分组，
    计算净冲击力，应用 Tanh 非线性平滑，生成交易建议。
    """

    @staticmethod
    def aggregate_daily_signals(
        llm_parsed_results: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """
        核心聚合函数。

        输入: LLM 各批次返回的 parsed JSON 列表
        输出: {
            ticker: {
                "news_count": int,
                "bullish_count": int,
                "bearish_count": int,
                "net_impact_raw": float,
                "final_smoothed_signal": float,
                "suggested_action": "Buy" | "Sell" | "Hold",
            },
            ...
        }
        """
        # ---- Step 1: 提取所有触发信号 ----
        ticker_signals: dict[str, list[dict[str, Any]]] = {}

        for batch_result in llm_parsed_results:
            if batch_result.get("_error"):
                continue
            for signal in batch_result.get("triggered_signals", []):
                headline = signal.get("news_headline", "")
                for asset in signal.get("impacted_assets", []):
                    ticker = (asset.get("ticker", "") or "").upper().strip()
                    direction = (asset.get("direction", "") or "").strip()
                    score_raw = asset.get("confidence_score", 5)
                    try:
                        score = int(score_raw)
                    except (ValueError, TypeError):
                        score = 5
                    score = max(1, min(10, score))

                    if ticker not in ticker_signals:
                        ticker_signals[ticker] = []

                    ticker_signals[ticker].append({
                        "direction": direction,
                        "confidence_score": score,
                        "headline": headline,
                    })

        # ---- Step 2: 聚合计算 ----
        aggregated: dict[str, dict[str, Any]] = {}

        for ticker, signals in ticker_signals.items():
            bullish_count = sum(1 for s in signals if s["direction"] == "Bullish")
            bearish_count = sum(1 for s in signals if s["direction"] == "Bearish")
            neutral_count = len(signals) - bullish_count - bearish_count

            net_impact = 0.0
            for s in signals:
                direction_val = (
                    1 if s["direction"] == "Bullish" else
                    -1 if s["direction"] == "Bearish" else
                    0
                )
                net_impact += direction_val * s["confidence_score"]

            # Tanh 非线性平滑
            final_smoothed = math.tanh(0.1 * net_impact)

            # 交易建议
            if final_smoothed > 0.3:
                action = "Buy"
            elif final_smoothed < -0.3:
                action = "Sell"
            else:
                action = "Hold"

            aggregated[ticker] = {
                "news_count": len(signals),
                "bullish_count": bullish_count,
                "bearish_count": bearish_count,
                "neutral_count": neutral_count,
                "net_impact_raw": round(net_impact, 4),
                "final_smoothed_signal": round(final_smoothed, 4),
                "suggested_action": action,
            }

        return aggregated

    @staticmethod
    def flatten_all_signals(
        llm_parsed_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将所有 LLM 提取的原始信号扁平化为列表，供后续审计。"""
        flat: list[dict[str, Any]] = []
        for batch_result in llm_parsed_results:
            batch_meta = batch_result.get("_batch_meta", {})
            for signal in batch_result.get("triggered_signals", []):
                flat.append({
                    "news_headline": signal.get("news_headline", ""),
                    "market_sector": signal.get("market_sector", ""),
                    "impacted_assets": signal.get("impacted_assets", []),
                    "_batch_id": batch_meta.get("batch_idx", "?"),
                })
        return flat


# =============================================================================
# ==================== LAYER 4: PORTFOLIO ALLOCATION ============================
# =============================================================================


class PortfolioAllocator:
    """
    资金分配与仓位优化器。
    根据 Layer 3 的聚合信号，按确信度（Conviction）动态分配资金，
    计算多空方向、仓位权重与目标持仓金额。
    """

    def __init__(
        self,
        signal_threshold: float = SIGNAL_THRESHOLD,
        total_capital: float = INITIAL_CAPITAL,
    ):
        self.threshold = signal_threshold
        self.total_capital = total_capital

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def calculate_portfolio_allocation(
        self,
        aggregated_signals: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        核心资金分配函数。

        输入: Layer 3 输出的 aggregated_signals 字典
        输出: 执行订单列表 [
            {
                "ticker": str,
                "action": "Long" | "Short",
                "final_signal": float,
                "allocation_weight": float,    # 0.0 ~ 1.0
                "target_dollar_amount": float,  # USD
            },
            ...
        ]

        若所有信号均不满足阈值，返回空列表（Hold Cash）。
        """
        # ---- Step 1: 过滤噪音信号 ----
        qualified: dict[str, dict[str, Any]] = {}
        for ticker, data in aggregated_signals.items():
            signal = data.get("final_smoothed_signal", 0.0)
            if abs(signal) >= self.threshold:
                qualified[ticker] = {**data, "final_smoothed_signal": signal}

        # ---- Step 2: 空仓观望检查 ----
        if not qualified:
            print("\n  [Portfolio] All signals below threshold → HOLD CASH ($%.2f)" % self.total_capital)
            return []

        # ---- Step 3: 确信度归一化 ----
        total_conviction = sum(abs(d["final_smoothed_signal"]) for d in qualified.values())

        # ---- Step 4: 计算权重与头寸 ----
        orders: list[dict[str, Any]] = []
        for ticker, data in qualified.items():
            signal = data["final_smoothed_signal"]
            conviction = abs(signal)
            weight = conviction / total_conviction
            direction = "Long" if signal > 0 else "Short"
            dollar_amount = round(self.total_capital * weight, 2)

            orders.append({
                "ticker": ticker,
                "action": direction,
                "final_signal": signal,
                "allocation_weight": round(weight, 4),
                "target_dollar_amount": dollar_amount,
            })

        # 按 dollar_amount 降序排列
        orders.sort(key=lambda x: x["target_dollar_amount"], reverse=True)

        return orders


def save_execution_orders(orders: list[dict[str, Any]]) -> str:
    """保存执行订单到 daily_execution_orders.csv。"""
    filepath = os.path.join(OUTPUT_DIR, "daily_execution_orders.csv")

    fieldnames = [
        "ticker",
        "action",
        "final_signal",
        "allocation_weight",
        "target_dollar_amount",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        if not orders:
            writer.writerow({
                "ticker": "CASH",
                "action": "Hold",
                "final_signal": 0.0,
                "allocation_weight": 1.0,
                "target_dollar_amount": INITIAL_CAPITAL,
            })
        else:
            for order in orders:
                writer.writerow(order)

    print(f"  [Saved] daily_execution_orders.csv → {filepath}")
    return filepath


def print_allocation_table(orders: list[dict[str, Any]]):
    """打印资金分配方案到控制台（美观表格）。"""
    print(f"\n{'=' * 80}")
    print(f"  PORTFOLIO ALLOCATION ORDERS  (Total Capital: ${INITIAL_CAPITAL:,.2f})")
    print(f"  {'=' * 78}")

    if not orders:
        print(f"  {'TICKER':<8} {'ACTION':>8} {'SIGNAL':>9} {'WEIGHT':>9} {'AMOUNT':>12}")
        print(f"  {'-' * 50}")
        print(f"  {'CASH':<8} {'Hold':>8} {'0.0000':>9} {'1.0000':>9} "
              f"{'$' + f'{INITIAL_CAPITAL:,.2f}':>12}")
        print(f"{'=' * 80}\n")
        return

    # 表头
    print(f"  {'TICKER':<8} {'ACTION':>8} {'SIGNAL':>9} {'WEIGHT':>9} {'AMOUNT':>12}")
    print(f"  {'-' * 50}")

    total_allocated = 0.0
    for order in orders:
        ticker = order["ticker"]
        action = order["action"]
        signal = order["final_signal"]
        weight = order["allocation_weight"]
        amount = order["target_dollar_amount"]
        total_allocated += amount

        action_str = f"▲ {action}" if action == "Long" else f"▼ {action}"
        print(f"  {ticker:<8} {action_str:>8} {signal:>9.4f} {weight:>9.4f} "
              f"{'$' + f'{amount:,.2f}':>12}")

    # 汇总行
    cash_left = round(INITIAL_CAPITAL - total_allocated, 2)
    print(f"  {'-' * 50}")
    print(f"  {'TOTAL':<8} {'':>8} {'':>9} {1.0:>9.4f} "
          f"{'$' + f'{total_allocated:,.2f}':>12}")
    print(f"{'=' * 80}\n")


# =============================================================================
# ==================== 输出层 ==================================================
# =============================================================================


def save_raw_analysis(llm_results: list[dict[str, Any]]) -> str:
    """保存 LLM 原始分析结果到 raw_llm_analysis.json。"""
    filepath = os.path.join(OUTPUT_DIR, "raw_llm_analysis.json")

    # 移除 _batch_meta 内的冗余文章内容以减小体积
    compact = []
    for batch in llm_results:
        item = dict(batch)
        if "_batch_meta" in item:
            item["_batch_meta"] = {
                "batch_idx": item["_batch_meta"].get("batch_idx"),
                "articles_count": len(item["_batch_meta"].get("articles_in_batch", [])),
            }
        compact.append(item)

    output = {
        "generated_at": datetime.now().isoformat(),
        "llm_model": LLM_MODEL,
        "batch_count": len(llm_results),
        "results": compact,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  [Saved] raw_llm_analysis.json → {filepath}")
    return filepath


def save_aggregated_signals(
    aggregated: dict[str, dict[str, Any]],
) -> str:
    """保存聚合信号到 daily_aggregated_signals.csv。"""
    filepath = os.path.join(OUTPUT_DIR, "daily_aggregated_signals.csv")

    fieldnames = [
        "ticker",
        "news_count",
        "bullish_count",
        "bearish_count",
        "neutral_count",
        "net_impact_raw",
        "final_smoothed_signal",
        "suggested_action",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ticker, data in sorted(aggregated.items()):
            row = {"ticker": ticker, **data}
            writer.writerow(row)

    print(f"  [Saved] daily_aggregated_signals.csv → {filepath}")
    return filepath


def print_signal_summary(aggregated: dict[str, dict[str, Any]]):
    """打印信号汇总到控制台。"""
    if not aggregated:
        print("\n  [Signal] No actionable signals generated today.")
        return

    print(f"\n{'=' * 70}")
    print(f"  DAILY SIGNAL SUMMARY")
    print(f"  {'Ticker':<8} {'News':>5} {'Bull':>5} {'Bear':>5} "
          f"{'NetRaw':>8} {'Smoothed':>9} {'Action':>7}")
    print(f"  {'-' * 65}")

    for ticker in sorted(aggregated.keys()):
        d = aggregated[ticker]
        action_icon = {"Buy": "▲ BUY", "Sell": "▼ SELL", "Hold": "─ Hold"}.get(
            d["suggested_action"], d["suggested_action"]
        )
        print(
            f"  {ticker:<8} {d['news_count']:>5} {d['bullish_count']:>5} "
            f"{d['bearish_count']:>5} {d['net_impact_raw']:>8.2f} "
            f"{d['final_smoothed_signal']:>9.4f} {action_icon:>7}"
        )
    print(f"{'=' * 70}\n")


# =============================================================================
# ==================== 主流程 (MAIN) ===========================================
# =============================================================================


def main():
    print("=" * 70)
    print("  Event-Driven Sentiment & Signal Aggregator")
    print("  事件驱动型资产情绪分析与信号聚合系统")
    print("=" * 70)
    print(f"  Search Engine : {'Tavily' if USE_TAVILY else 'DuckDuckGo'}")
    print(f"  LLM Model     : {LLM_MODEL}")
    print(f"  Time Window   : past {SEARCH_DAYS_BACK} days")
    print(f"  Output Dir    : {OUTPUT_DIR}")
    print("=" * 70)

    # =====================================================================
    # STEP 1: 数据获取 — 事件驱动广度搜索
    # =====================================================================
    print(f"\n{'─' * 70}")
    print("  LAYER 1: Event-Driven News Acquisition")
    print(f"{'─' * 70}")
    fetcher = NewsDataFetcher()
    news_stream = fetcher.build_news_stream()

    if not news_stream:
        print("\n  [ABORT] No news fetched. Check network / search engine config.")
        return

    # 可选：保存原始新闻流供调试
    news_stream_path = os.path.join(OUTPUT_DIR, "raw_news_stream.json")
    with open(news_stream_path, "w", encoding="utf-8") as f:
        json.dump(news_stream, f, ensure_ascii=False, indent=2, default=str)

    # =====================================================================
    # STEP 2: LLM 分析 — 动态标的识别与情绪打分
    # =====================================================================
    print(f"\n{'─' * 70}")
    print("  LAYER 2: LLM Sentiment Analysis")
    print(f"{'─' * 70}")
    analyzer = LLMSentimentAnalyzer()
    llm_results = analyzer.analyze_stream(news_stream)

    # 统计
    total_signals = sum(
        len(b.get("triggered_signals", [])) for b in llm_results if not b.get("_error")
    )
    moving_batches = sum(
        1 for b in llm_results
        if b.get("has_market_moving_news") and not b.get("_error")
    )
    print(f"\n  [Summary] {len(llm_results)} batches processed, "
          f"{moving_batches} with signals, {total_signals} total triggered_signals")

    # =====================================================================
    # STEP 3: 信号聚合与平滑
    # =====================================================================
    print(f"\n{'─' * 70}")
    print("  LAYER 3: Signal Aggregation & Smoothing")
    print(f"{'─' * 70}")
    aggregated = SignalAggregator.aggregate_daily_signals(llm_results)
    print(f"  Aggregated signals for {len(aggregated)} unique tickers.")

    # =====================================================================
    # STEP 4: 资金分配与仓位优化
    # =====================================================================
    print(f"\n{'─' * 70}")
    print("  LAYER 4: Portfolio Allocation & Sizing")
    print(f"{'─' * 70}")
    allocator = PortfolioAllocator()
    orders = allocator.calculate_portfolio_allocation(aggregated)
    print(f"  Generated {len(orders)} execution orders.")
    print_allocation_table(orders)

    # =====================================================================
    # STEP 5: 输出 — 保存所有结果文件
    # =====================================================================
    print(f"\n{'─' * 70}")
    print("  OUTPUT: Saving all result files...")
    print(f"{'─' * 70}")

    save_raw_analysis(llm_results)
    save_aggregated_signals(aggregated)
    save_execution_orders(orders)
    print_signal_summary(aggregated)

    print("  [DONE] System run complete.\n")


if __name__ == "__main__":
    main()
