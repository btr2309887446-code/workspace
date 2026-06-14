"""
新闻宏观情绪引擎 (news_engine.py)
===================================
职责：
  1. 异步获取实时金融新闻（Alpaca News WebSocket / RSS 轮询）
  2. 将新闻标题+摘要异步发送给 LLM 进行情绪评估
  3. 维护带时间衰减的宏观情绪状态池 (Macro Sentiment Pool)
  4. 向 analytics.py / llm_agent.py 注入当前宏观偏置 bias

架构：
  Alpaca WS / RSS → RealTimeNewsFetcher → LLM 情绪评估
                                              │
                                    Macro Sentiment Pool
                                    {ticker: bias_score}
                                              │
                                    get_current_bias(ticker)
                                              │
                                    analytics.get_snapshot(bias=...)
                                              │
                                    llm_agent 风控裁决

情绪衰减公式：
  pool[t] = pool[t-1] × e^(-λ × Δt) + new_score × (1 - e^(-λ × Δt))
  其中 λ = ln(2) / half_life  (半衰期默认 30 分钟)
"""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Any, List

import aiohttp

logger = logging.getLogger("SwapMomentum.NewsEngine")


# ============================================================================
# 情绪池条目
# ============================================================================

@dataclass
class _SentimentEntry:
    """单个标的的情绪状态"""
    score: float = 0.0           # 当前聚合情绪 (-1.0 ~ 1.0)
    article_count: int = 0       # 已处理文章数
    last_update: float = 0.0     # 最后更新时间戳
    last_headline: str = ""      # 最新文章标题


# ============================================================================
# 宏观情绪状态池
# ============================================================================

class MacroSentimentPool:
    """
    带时间衰减的宏观情绪状态池（单例模式）。

    特性：
      - 指数时间衰减：旧情绪随时间自然消退
      - 增量更新：新文章通过加权平滑合并
      - 线程安全：asyncio 单线程模型下天然安全
    """

    def __init__(self, half_life_seconds: float = 1800.0):
        """
        初始化情绪池。

        Args:
            half_life_seconds: 情绪半衰期（秒），默认 30 分钟。
                               30 分钟后旧情绪权重衰减至 50%。
        """
        self._pool: Dict[str, _SentimentEntry] = {}
        self._half_life = half_life_seconds
        self._decay_lambda = math.log(2) / half_life_seconds  # λ = ln2 / T½

        # 默认监控的标的
        self._default_tickers = {
            "TSLA", "NVDA", "AAPL", "MU", "WDC", "MRVL",
            "SAMSUNG", "SKHYNIX",
        }

        # 统计
        self.stats = {
            "articles_processed": 0,
            "llm_calls": 0,
            "llm_failures": 0,
            "last_article_ts": 0.0,
        }

        # 后台衰减任务
        self._decay_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_bias(self, ticker: str) -> float:
        """
        获取某标的的当前宏观情绪值（含时间衰减）。

        Args:
            ticker: 标的代码（如 "TSLA" 或 "TSLA-USDT-SWAP"）

        Returns:
            情绪值 ∈ [-1.0, 1.0]，0 表示中性
        """
        ticker = self._normalize(ticker)
        entry = self._pool.get(ticker)
        if entry is None:
            return 0.0
        return self._apply_decay(entry)

    def get_all_bias(self) -> Dict[str, float]:
        """获取全部标的的当前情绪值（含衰减）。"""
        return {t: self.get_bias(t) for t in list(self._pool.keys())}

    def update(self, ticker: str, impact_score: float, headline: str = "") -> None:
        """
        将新的情绪评分合并到情绪池。

        合并公式：
          score_new = score_old × retention + impact × (1 - retention)
          其中 retention = e^(-λ × Δt)

        Args:
            ticker:       标的代码
            impact_score: LLM 输出的 -1.0 ~ 1.0 分数
            headline:     文章标题（用于日志）
        """
        ticker = self._normalize(ticker)
        now = time.time()

        if ticker not in self._pool:
            entry = _SentimentEntry(
                score=impact_score,
                article_count=1,
                last_update=now,
                last_headline=headline,
            )
            self._pool[ticker] = entry
        else:
            entry = self._pool[ticker]
            # 先用时间衰减更新旧值
            decayed = self._apply_decay(entry)
            dt = now - entry.last_update
            retention = math.exp(-self._decay_lambda * dt) if dt > 0 else 1.0
            # 加权合并
            entry.score = decayed * retention + impact_score * (1.0 - retention)
            entry.article_count += 1
            entry.last_update = now
            entry.last_headline = headline

        self.stats["articles_processed"] += 1

    async def start_decay_task(self, interval: float = 60.0) -> None:
        """
        启动后台衰减任务。

        每 interval 秒遍历一次所有标的，衰减其情绪值。
        防止情绪池无限膨胀。

        Args:
            interval: 衰减检查间隔（秒）
        """
        self._running = True
        self._decay_task = asyncio.create_task(
            self._decay_loop(interval), name="SentimentDecay"
        )
        logger.info(
            f"情绪衰减任务已启动 | half_life={self._half_life:.0f}s | "
            f"interval={interval}s"
        )

    async def stop_decay_task(self) -> None:
        """停止后台衰减任务。"""
        self._running = False
        if self._decay_task and not self._decay_task.done():
            self._decay_task.cancel()
            try:
                await self._decay_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _apply_decay(self, entry: _SentimentEntry) -> float:
        """
        对单个情绪条目应用时间衰减。

        衰减公式：score = score × e^(-λ × Δt)
        """
        dt = time.time() - entry.last_update
        if dt <= 0:
            return entry.score
        return entry.score * math.exp(-self._decay_lambda * dt)

    async def _decay_loop(self, interval: float) -> None:
        """后台衰减循环。"""
        while self._running:
            try:
                await asyncio.sleep(interval)
                # 对每个标的应用衰减
                for ticker, entry in list(self._pool.items()):
                    decayed = self._apply_decay(entry)
                    # 若绝对值小于阈值，视为已消退，清除记录
                    if abs(decayed) < 0.01:
                        del self._pool[ticker]
                    else:
                        entry.score = decayed
                        entry.last_update = time.time()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("情绪衰减循环异常")

    @staticmethod
    def _normalize(ticker: str) -> str:
        """标准化标的代码：TSLA-USDT-SWAP → TSLA"""
        return ticker.split("-")[0] if "-" in ticker else ticker

    @property
    def ticker_count(self) -> int:
        return len(self._pool)


# ============================================================================
# 实时新闻获取器
# ============================================================================

class RealTimeNewsFetcher:
    """
    实时新闻获取器。

    支持两种数据源（自动降级）：
      1. Alpaca News WebSocket（需 API Key，低延迟）
      2. 公开 RSS 订阅源轮询（零配置，延迟 60s）
    """

    # 公开 RSS 订阅源（金融/加密）
    RSS_FEEDS = [
        {
            "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA"
                   ",NVDA,AAPL,MU,WDC,MRVL&region=US&lang=en-US",
            "type": "stocks",
        },
        {
            "url": "https://www.coindesk.com/arc/outboundfeeds/news-rss/",
            "type": "crypto",
        },
        {
            "url": "https://cointelegraph.com/rss",
            "type": "crypto",
        },
    ]

    def __init__(
        self,
        sentiment_pool: MacroSentimentPool,
        llm_endpoint: str = "",
        llm_api_key: str = "",
        llm_model: str = "gpt-4o-mini",
        use_rss_only: bool = True,
        poll_interval: float = 60.0,
    ):
        """
        初始化新闻获取器。

        Args:
            sentiment_pool:  宏观情绪池实例
            llm_endpoint:    LLM API 端点
            llm_api_key:     LLM API Key
            llm_model:       LLM 模型名称
            use_rss_only:    仅使用 RSS（无 API Key 时自动启用）
            poll_interval:   RSS 轮询间隔（秒）
        """
        self.sentiment_pool = sentiment_pool
        self.llm_endpoint = llm_endpoint
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self.use_rss_only = use_rss_only
        self.poll_interval = poll_interval

        self._running = False
        self._fetcher_task: Optional[asyncio.Task] = None

        # 已处理 URL 去重
        self._seen_urls: set = set()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动新闻获取任务。"""
        self._running = True
        # RSS 轮询模式
        self._fetcher_task = asyncio.create_task(
            self._rss_poll_loop(), name="NewsFetcher"
        )
        logger.info(
            f"新闻获取器已启动 | mode=RSS | interval={self.poll_interval}s"
        )

    async def stop(self) -> None:
        """停止新闻获取。"""
        self._running = False
        if self._fetcher_task and not self._fetcher_task.done():
            self._fetcher_task.cancel()
            try:
                await self._fetcher_task
            except asyncio.CancelledError:
                pass
        logger.info("新闻获取器已停止")

    # ------------------------------------------------------------------
    # 内部：RSS 轮询
    # ------------------------------------------------------------------

    async def _rss_poll_loop(self) -> None:
        """RSS 轮询主循环。"""
        logger.info("RSS 新闻轮询已启动")
        while self._running:
            try:
                for feed in self.RSS_FEEDS:
                    if not self._running:
                        break
                    articles = await self._fetch_rss(feed["url"])
                    for article in articles:
                        if article["url"] not in self._seen_urls:
                            self._seen_urls.add(article["url"])
                            # 每篇文章异步 LLM 分析（不阻塞其他文章）
                            asyncio.create_task(
                                self._analyze_article(article)
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("RSS 轮询异常")

            await asyncio.sleep(self.poll_interval)

        logger.info("RSS 新闻轮询已退出")

    async def _fetch_rss(self, url: str) -> List[Dict[str, str]]:
        """
        异步抓取 RSS 源，提取文章列表。

        Returns:
            [{"title": ..., "summary": ..., "url": ..., "published": ...}, ...]
        """
        import xml.etree.ElementTree as ET

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "QuantBot/1.0"},
                ) as resp:
                    if resp.status != 200:
                        return []
                    xml_text = await resp.text()

            root = ET.fromstring(xml_text)
            articles = []
            # RSS 2.0 格式
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                summary = item.findtext("description", "")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate", "")

                # 清理 HTML 标签
                if summary:
                    summary = re.sub(r"<[^>]+>", "", summary)

                if title and link:
                    articles.append({
                        "title": title.strip(),
                        "summary": summary.strip()[:500] if summary else "",
                        "url": link.strip(),
                        "published": pub_date,
                    })

            return articles

        except Exception:
            logger.debug(f"RSS 抓取失败: {url}")
            return []

    # ------------------------------------------------------------------
    # 内部：LLM 情绪评估
    # ------------------------------------------------------------------

    # 情绪评估 System Prompt
    SENTIMENT_PROMPT = """你是一个金融新闻情绪分析师。

分析给定的新闻标题和摘要，输出纯 JSON（禁止其他任何文字）：

{"impact_score": 0.0, "relevant_tickers": ["TSLA", "AAPL"], "event_type": "product"}

字段说明：
  impact_score: 对股价的影响程度，-1.0（极度利空）到 1.0（极度利好），0.0 为中性
  relevant_tickers: 受影响的股票代码列表，如 ["TSLA","NVDA","AAPL"]
  event_type: earnings（财报）/ macro（宏观）/ scandal（丑闻）/ product（产品）/ legal（法律）/ geopol（地缘）/ other
"""

    async def _analyze_article(self, article: Dict[str, str]) -> None:
        """
        将单篇文章发送给 LLM 进行情绪评估。

        评估完成后自动更新 MacroSentimentPool。
        """
        if not self.llm_api_key or not self.llm_endpoint:
            return

        self.sentiment_pool.stats["llm_calls"] += 1

        try:
            result = await self._call_llm(
                title=article["title"],
                summary=article.get("summary", ""),
            )
            if result is None:
                self.sentiment_pool.stats["llm_failures"] += 1
                return

            impact = result.get("impact_score", 0.0)
            tickers = result.get("relevant_tickers", [])
            event_type = result.get("event_type", "other")

            # 钳制到 [-1, 1]
            impact = max(-1.0, min(1.0, float(impact)))

            if tickers:
                for ticker in tickers:
                    self.sentiment_pool.update(
                        ticker=str(ticker).upper(),
                        impact_score=impact,
                        headline=article["title"],
                    )
                logger.info(
                    f"新闻情绪 | {','.join(tickers)} | "
                    f"score={impact:+.2f} | type={event_type} | "
                    f"{article['title'][:60]}"
                )
            else:
                # 无特定标的时，影响全局情绪
                for t in self.sentiment_pool._default_tickers:
                    self.sentiment_pool.update(
                        ticker=t,
                        impact_score=impact * 0.5,  # 泛化新闻减半权重
                        headline=article["title"],
                    )

            self.sentiment_pool.stats["last_article_ts"] = time.time()

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"文章分析异常: {article.get('title', '')[:60]}")
            self.sentiment_pool.stats["llm_failures"] += 1

    async def _call_llm(
        self, title: str, summary: str
    ) -> Optional[Dict[str, Any]]:
        """
        调用 LLM API 进行情绪评估。

        Returns:
            {"impact_score": float, "relevant_tickers": [...], "event_type": str}
        """
        user_prompt = (
            f"新闻标题: {title}\n"
            f"新闻摘要: {summary[:300] if summary else '无'}\n"
            f"请输出 JSON 评估。"
        )

        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": self.SENTIMENT_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 200,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.llm_api_key}",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.llm_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    return self._parse_sentiment_json(content)

        except asyncio.TimeoutError:
            logger.warning("LLM 情绪评估超时")
        except Exception:
            logger.exception("LLM 情绪评估异常")

        return None

    @staticmethod
    def _parse_sentiment_json(text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的情绪 JSON。"""
        if not text:
            return None

        # 剥离 markdown
        text = text.strip()
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                text = text.replace("'", '"')
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None

        if not isinstance(parsed, dict):
            return None

        impact = parsed.get("impact_score", 0.0)
        try:
            impact = float(impact)
        except (ValueError, TypeError):
            impact = 0.0

        tickers = parsed.get("relevant_tickers", [])
        if isinstance(tickers, str):
            tickers = [t.strip() for t in tickers.split(",")]

        return {
            "impact_score": max(-1.0, min(1.0, impact)),
            "relevant_tickers": list(tickers),
            "event_type": parsed.get("event_type", "other"),
        }


# ============================================================================
# 全局单例工厂
# ============================================================================

_global_pool: Optional[MacroSentimentPool] = None
_global_fetcher: Optional[RealTimeNewsFetcher] = None


def get_sentiment_pool(
    half_life_seconds: float = 1800.0,
) -> MacroSentimentPool:
    """获取全局情绪池单例。"""
    global _global_pool
    if _global_pool is None:
        _global_pool = MacroSentimentPool(half_life_seconds)
    return _global_pool


def get_current_bias(ticker: str) -> float:
    """
    便捷函数：获取某个标的的当前宏观情绪值。

    用法（在 analytics.py 或 llm_agent.py 中）：
        from news_engine import get_current_bias
        bias = get_current_bias("TSLA")

    Returns:
        -1.0 ~ 1.0 的情绪值
    """
    pool = get_sentiment_pool()
    return pool.get_bias(ticker)


# ============================================================================
# 集成指南
# ============================================================================

"""
=== 如何将 news_engine 接入现有系统 ===

1. 在 pipeline.py 的 main() 中初始化：

    from news_engine import get_sentiment_pool, RealTimeNewsFetcher

    # 在 db_manager 初始化之后
    pool = get_sentiment_pool(half_life_seconds=1800)
    await pool.start_decay_task()

    if settings.llm_configured:
        fetcher = RealTimeNewsFetcher(
            sentiment_pool=pool,
            llm_endpoint=settings.llm_api_endpoint,
            llm_api_key=settings.llm_api_key,
            llm_model=settings.llm_model,
        )
        await fetcher.start()

    # 停机时
    await fetcher.stop()
    await pool.stop_decay_task()


2. 在 analytics.py 的 get_recent_snapshot() 中注入 bias：

    from news_engine import get_current_bias

    def get_recent_snapshot(self, n=10):
        # ... 现有逻辑 ...
        bias = get_current_bias("TSLA")  # 从 ticker 提取
        return {
            # ... 现有字段 ...
            "macro_sentiment": bias,
            "sentiment_signal": (
                "宏观利好" if bias > 0.3 else
                "宏观利空" if bias < -0.3 else "宏观中性"
            ),
        }

3. 在 llm_agent.py 的 _build_prompt 中展示 bias：

    bias = five_min_stats.get("macro_sentiment", 0)
    signal = five_min_stats.get("sentiment_signal", "未知")
    lines.append(f"宏观情绪: {bias:+.2f} ({signal})")

这样 LLM 在做动能裁判时能看到：
  - 微观层：velocity/energy/Z-Score/OBI/RSI（来自 analytics）
  - 宏观层：macro_sentiment（来自 news_engine）
"""


# ============================================================================
# 模拟测试
# ============================================================================

async def _run_demo():
    """
    独立演示：新闻情绪引擎完整工作流。

    使用 Mock LLM（无 API Key 也可运行）。
    """
    print("=" * 64)
    print("  News Sentiment Engine — Demo")
    print("=" * 64)

    # 1. 创建情绪池
    pool = MacroSentimentPool(half_life_seconds=30)  # 30s 半衰期便于观察衰减
    print(f"\n  Pool created: half_life={pool._half_life}s")

    # 2. 模拟 LLM 分析结果直接注入
    print("\n  ── Simulating news articles ──")

    # TSLA 新产品发布 → 强利好
    pool.update("TSLA", 0.85, "特斯拉发布新一代自动驾驶芯片")
    print(f"    TSLA after chip news: {pool.get_bias('TSLA'):+.3f}")

    # TSLA 供应链问题 → 利空
    pool.update("TSLA", -0.60, "特斯拉上海工厂产能受阻")
    print(f"    TSLA after supply chain: {pool.get_bias('TSLA'):+.3f}")

    # NVDA 财报超预期 → 利好
    pool.update("NVDA", 0.95, "英伟达Q4财报超预期，盘后涨12%")
    print(f"    NVDA after earnings: {pool.get_bias('NVDA'):+.3f}")

    # AAPL 反垄断调查 → 利空
    pool.update("AAPL", -0.70, "苹果面临欧盟反垄断调查")
    print(f"    AAPL after antitrust: {pool.get_bias('AAPL'):+.3f}")

    # 获取全局情绪
    print(f"\n  All biases: {pool.get_all_bias()}")
    assert pool.get_bias("TSLA") != pool.get_bias("NVDA")  # 独立跟踪
    print("  [PASS] Independent ticker tracking")

    # 3. 测试时间衰减
    print("\n  ── Testing time decay ──")
    await pool.start_decay_task(interval=5)  # 每 5 秒衰减
    print("    Decay task started (5s interval)")
    await asyncio.sleep(8)  # 等 8 秒让衰减生效

    bias_after = pool.get_bias("NVDA")
    print(f"    NVDA after 8s decay: {bias_after:+.3f} (was +0.950)")
    assert bias_after < 0.95  # 应该衰减了
    print("  [PASS] Time decay working")

    await pool.stop_decay_task()

    # 4. 测试 normalize
    assert pool._normalize("TSLA-USDT-SWAP") == "TSLA"
    print(f"\n  [PASS] Ticker normalize: TSLA-USDT-SWAP → TSLA")

    # 5. 测试 JSON 解析
    print("\n  ── Testing JSON parser ──")
    r1 = RealTimeNewsFetcher._parse_sentiment_json(
        '```json\n{"impact_score":0.75,"relevant_tickers":["TSLA"],"event_type":"product"}\n```'
    )
    assert r1["impact_score"] == 0.75
    assert "TSLA" in r1["relevant_tickers"]
    print(f"    Markdown JSON: score={r1['impact_score']}, tickers={r1['relevant_tickers']}")
    print("  [PASS]")

    r2 = RealTimeNewsFetcher._parse_sentiment_json(
        '{"impact_score": -0.5, "relevant_tickers": "NVDA,AAPL"}'
    )
    assert r2["impact_score"] == -0.5
    assert r2["relevant_tickers"] == ["NVDA", "AAPL"]
    print(f"    String tickers: {r2['relevant_tickers']}")
    print("  [PASS]")

    # 6. 统计
    print(f"\n  Pool stats: {pool.stats}")
    print(f"  Active tickers: {pool.ticker_count}")

    print("\n" + "=" * 64)
    print("  DEMO COMPLETE")
    print("=" * 64)


if __name__ == "__main__":
    try:
        asyncio.run(_run_demo())
    except KeyboardInterrupt:
        print("\nInterrupted.")
