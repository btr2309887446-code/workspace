"""
历史新闻情绪预处理管道 (historical_news_pipeline.py)
=====================================================
职责：
  1. 从 Alpaca 历史新闻 API 批量拉取指定标的的新闻
  2. 使用 LLM 批量打分（高并发 + Semaphore + 断点续传）
  3. 输出 Parquet 文件供回测引擎直接读取

数据流：
  Alpaca API → 新闻列表 → SQLite checkpoint → LLM 打分(Semaphore=5)
  → SQLite 更新 → pandas → Parquet

断点续传机制：
  每条新闻的 URL 作为唯一键，已处理的记录在 SQLite 中标记。
  重跑时跳过已打分新闻，仅处理新数据。
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm as async_tqdm

logger = logging.getLogger("HistoricalNewsPipeline")

# 输出目录
os.makedirs("data/historical", exist_ok=True)
CHECKPOINT_DB = "data/historical/_news_checkpoint.db"
OUTPUT_PARQUET = "data/historical/news_sentiment.parquet"


# ============================================================================
# SQLite 断点续传管理器
# ============================================================================

class CheckpointManager:
    """
    SQLite 断点续传管理器。

    表结构：
      news_checkpoint
        url       TEXT PRIMARY KEY   — 新闻 URL（唯一键）
        ticker    TEXT               — 标的代码
        headline  TEXT               — 标题
        timestamp TEXT               — 发布时间
        impact    REAL               — 情绪分数（NULL=未处理）
        processed INTEGER DEFAULT 0  — 0=待处理, 1=已完成
    """

    def __init__(self, db_path: str = CHECKPOINT_DB):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        """打开或创建 SQLite 数据库。"""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS news_checkpoint (
                url        TEXT PRIMARY KEY,
                ticker     TEXT NOT NULL,
                headline   TEXT,
                timestamp  TEXT,
                impact     REAL,
                event_type TEXT,
                processed  INTEGER DEFAULT 0
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed
            ON news_checkpoint (processed)
        """)
        self._conn.commit()
        logger.info(f"断点管理器就绪 | db={self.db_path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def insert_news(self, articles: List[Dict[str, str]]) -> int:
        """
        批量插入新闻（已存在的 URL 忽略）。

        Returns:
            新插入的条数
        """
        count = 0
        for a in articles:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO news_checkpoint (url, ticker, headline, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (a["url"], a["ticker"], a["headline"], a["timestamp"]),
                )
                count += self._conn.total_changes
            except Exception:
                pass
        self._conn.commit()
        # 重新计数
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM news_checkpoint WHERE processed = 0"
        )
        return cursor.fetchone()[0]

    def get_pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取待处理的新闻列表。"""
        cursor = self._conn.execute(
            "SELECT url, ticker, headline, timestamp FROM news_checkpoint "
            "WHERE processed = 0 LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        return [
            {"url": r[0], "ticker": r[1], "headline": r[2], "timestamp": r[3]}
            for r in rows
        ]

    def mark_done(self, url: str, impact: float, event_type: str = "") -> None:
        """标记一条新闻为已处理。"""
        self._conn.execute(
            "UPDATE news_checkpoint SET impact=?, event_type=?, processed=1 "
            "WHERE url=?",
            (impact, event_type, url),
        )
        self._conn.commit()

    @property
    def stats(self) -> Dict[str, int]:
        cursor = self._conn.execute(
            "SELECT processed, COUNT(*) FROM news_checkpoint GROUP BY processed"
        )
        rows = cursor.fetchall()
        return {f"{'done' if r[0] else 'pending'}": r[1] for r in rows}

    def to_dataframe(self) -> pd.DataFrame:
        """将已处理的数据导出为 DataFrame。"""
        return pd.read_sql_query(
            "SELECT timestamp, ticker, headline, impact, event_type "
            "FROM news_checkpoint WHERE processed = 1",
            self._conn,
        )


# ============================================================================
# 异步新闻爬虫
# ============================================================================

class AlpacaNewsCrawler:
    """
    Alpaca 历史新闻 API 爬虫。

    使用 Alpaca Data API v2 的历史新闻接口，
    通过 API Key + Secret 鉴权，分页拉取。
    """

    NEWS_API = "https://data.alpaca.markets/v1beta1/news"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        max_retries: int = 3,
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.max_retries = max_retries
        self.timeout = timeout

    async def fetch(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        max_articles: int = 5000,
    ) -> List[Dict[str, str]]:
        """
        拉取指定标的在日期区间内的历史新闻。

        Args:
            symbols:       股票代码列表 ["TSLA", "AAPL"]
            start_date:    起始日期 "2026-01-01"
            end_date:      截止日期 "2026-01-31"
            max_articles:  最大文章数

        Returns:
            [{"url": ..., "ticker": ..., "headline": ..., "timestamp": ...}, ...]
        """
        all_articles = []
        page_token = None
        symbols_str = ",".join(symbols)

        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

        logger.info(
            f"开始拉取 Alpaca 新闻 | symbols={symbols_str} | "
            f"{start_date} ~ {end_date}"
        )

        while len(all_articles) < max_articles:
            params = {
                "symbols": symbols_str,
                "start": f"{start_date}T00:00:00Z",
                "end": f"{end_date}T23:59:59Z",
                "limit": 50,
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token

            # 带重试的 HTTP 请求
            data = await self._fetch_with_retry(
                self.NEWS_API, params, headers
            )
            if data is None:
                break

            news_list = data.get("news", [])
            if not news_list:
                break

            for n in news_list:
                syms = n.get("symbols", [])
                # 确定主标的
                primary = syms[0] if syms else "UNKNOWN"
                published = n.get("updated_at") or n.get("created_at") or ""
                all_articles.append({
                    "url": n.get("url") or n.get("id", ""),
                    "ticker": primary,
                    "headline": (n.get("headline", "") or "")[:500],
                    "timestamp": published,
                })

            page_token = data.get("next_page_token")
            if not page_token:
                break

            # 礼貌间隔
            await asyncio.sleep(0.2)

        logger.info(f"拉取完成 | total={len(all_articles)}")
        return all_articles

    async def _fetch_with_retry(
        self, url: str, params: dict, headers: dict
    ) -> Optional[dict]:
        """带指数退避重试的 GET 请求。"""
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        elif resp.status == 429:
                            # Rate Limit → 等更久
                            wait = 5 * (2 ** attempt)
                            logger.warning(
                                f"Rate limited, waiting {wait}s..."
                            )
                            await asyncio.sleep(wait)
                        elif resp.status == 404:
                            return None
                        else:
                            logger.warning(
                                f"HTTP {resp.status} (attempt {attempt})"
                            )
            except asyncio.TimeoutError:
                logger.warning(f"超时 (attempt {attempt})")
            except aiohttp.ClientError as e:
                logger.warning(f"网络错误: {e} (attempt {attempt})")

            if attempt < self.max_retries:
                delay = 2.0 * (2 ** (attempt - 1))
                delay *= 0.5 + random.random()
                await asyncio.sleep(delay)

        return None


# ============================================================================
# LLM 批量打分管道
# ============================================================================

class NewsScoringPipeline:
    """
    LLM 批量情绪打分管道。

    特性：
      - asyncio.Semaphore 控制并发量
      - SQLite 断点续传：每处理完一条立即标记
      - tqdm 进度条
      - 超时/异常自动跳过，不影响其他新闻
    """

    SENTIMENT_PROMPT = """你是一个金融新闻情绪分析师。
分析以下新闻标题，输出纯 JSON（禁止其他文字）：

{"impact_score": 0.0, "event_type": "other"}

impact_score: -1.0(极度利空) ~ 1.0(极度利好)
event_type: earnings/macro/scandal/product/legal/geopol/other"""

    def __init__(
        self,
        checkpoint: CheckpointManager,
        llm_endpoint: str,
        llm_api_key: str,
        llm_model: str = "gpt-4o-mini",
        max_concurrent: int = 10,
        timeout: float = 10.0,
    ):
        self.checkpoint = checkpoint
        self.llm_endpoint = llm_endpoint
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self.max_concurrent = max_concurrent
        self.timeout = timeout

        self.semaphore = asyncio.Semaphore(max_concurrent)

        # 统计
        self.scored = 0
        self.failed = 0

    async def run(self) -> Dict[str, int]:
        """执行批量打分，直到所有待处理新闻完成。"""
        logger.info(
            f"LLM 批量打分启动 | concurrent={self.max_concurrent}"
        )

        total = 0
        pending = self.checkpoint.get_pending(1)
        if pending:
            # 获取总数
            from collections import Counter
            c = Counter()
            c.update(self.checkpoint.stats)
            total = c.get("pending", 0)

        if total == 0:
            logger.info("无待处理新闻，跳过")
            return {"scored": 0, "failed": 0}

        # 构建任务队列
        tasks = []
        batch = self.checkpoint.get_pending(total)
        for article in batch:
            tasks.append(self._score_one(article))

        # 并发执行
        for coro in async_tqdm.as_completed(
            tasks,
            desc="LLM Scoring",
            total=len(tasks),
            unit="news",
        ):
            try:
                await coro
            except Exception:
                pass

        logger.info(
            f"批量打分完成 | scored={self.scored} failed={self.failed}"
        )
        return {"scored": self.scored, "failed": self.failed}

    async def _score_one(self, article: Dict[str, Any]) -> None:
        """对单条新闻打分并标记。"""
        async with self.semaphore:
            url = article["url"]
            headline = article["headline"]

            try:
                impact, event_type = await self._call_llm(headline)

                self.checkpoint.mark_done(url, impact, event_type)
                self.scored += 1

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"打分失败: {headline[:40]}... error={e}")
                # 仍然标记为已处理（impact=0 中性），防止死循环
                self.checkpoint.mark_done(url, 0.0, "other")
                self.failed += 1

    async def _call_llm(self, headline: str) -> Tuple[float, str]:
        """
        调用 LLM API 对单条新闻打分。

        Returns:
            (impact_score, event_type)
        """
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": self.SENTIMENT_PROMPT},
                {"role": "user", "content": f"新闻: {headline}"},
            ],
            "temperature": 0.0,
            "max_tokens": 100,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.llm_api_key}",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.llm_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                data = await resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return self._parse_score(content)

    @staticmethod
    def _parse_score(text: str) -> Tuple[float, str]:
        """解析 LLM 返回的情绪 JSON。"""
        if not text:
            return 0.0, "other"

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
                parsed = json.loads(text.replace("'", '"'))
            except json.JSONDecodeError:
                return 0.0, "other"

        impact = parsed.get("impact_score", 0.0)
        try:
            impact = float(impact)
        except (ValueError, TypeError):
            impact = 0.0

        return max(-1.0, min(1.0, impact)), parsed.get("event_type", "other")


# ============================================================================
# 主管道
# ============================================================================

async def run_pipeline(
    symbols: List[str],
    start_date: str,
    end_date: str,
    alpaca_key: str,
    alpaca_secret: str,
    llm_endpoint: str = "",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    max_concurrent: int = 10,
) -> str:
    """
    执行完整的历史新闻情绪预处理管道。

    1. 爬取历史新闻 → 写入 SQLite
    2. LLM 批量打分 → 更新 SQLite
    3. 导出 Parquet

    Returns:
        输出的 parquet 文件路径
    """
    # 1. 断点管理器
    ck = CheckpointManager()
    ck.open()

    try:
        # 2. 爬取新闻
        crawler = AlpacaNewsCrawler(alpaca_key, alpaca_secret)
        articles = await crawler.fetch(symbols, start_date, end_date)

        if articles:
            pending = ck.insert_news(articles)
            logger.info(f"新闻入库完成 | total={len(articles)} pending={pending}")
        else:
            logger.warning("未拉取到任何新闻")

        # 3. LLM 打分
        if llm_endpoint and llm_api_key:
            pipeline = NewsScoringPipeline(
                checkpoint=ck,
                llm_endpoint=llm_endpoint,
                llm_api_key=llm_api_key,
                llm_model=llm_model,
                max_concurrent=max_concurrent,
            )
            result = await pipeline.run()
            logger.info(f"LLM 打分结果: {result}")
        else:
            logger.warning("LLM 未配置，跳过打分步骤")

        # 4. 导出 Parquet
        stats = ck.stats
        if stats.get("done", 0) > 0:
            df = ck.to_dataframe()
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(
                    df["timestamp"], errors="coerce", utc=True
                ).astype("int64") // 10**9
                df["timestamp"] = df["timestamp"].astype(float)
            df.to_parquet(OUTPUT_PARQUET, index=False)
            logger.info(
                f"Parquet 导出完成 | rows={len(df)} | path={OUTPUT_PARQUET}"
            )
        else:
            logger.warning("无已打分数据，跳过 Parquet 导出")

        return OUTPUT_PARQUET

    finally:
        ck.close()


# ============================================================================
# 命令行入口
# ============================================================================

async def main():
    """默认示例：拉取 TSLA/AAPL/NVDA 过去 30 天新闻并打分。"""
    alpaca_key = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_API_SECRET", "")
    llm_key = os.getenv("LLM_API_KEY", "")
    llm_endpoint = os.getenv("LLM_API_ENDPOINT", "https://api.openai.com/v1/chat/completions")

    if not alpaca_key:
        print("请设置环境变量 ALPACA_API_KEY 和 ALPACA_API_SECRET")
        return

    end = datetime.utcnow()
    start = end - timedelta(days=30)

    path = await run_pipeline(
        symbols=["TSLA", "AAPL", "NVDA", "MU", "WDC", "MRVL"],
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        llm_endpoint=llm_endpoint if llm_key else "",
        llm_api_key=llm_key,
    )

    print(f"\n输出文件: {path}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) >= 5:
        # CLI: python historical_news_pipeline.py TSLA,AAPL 2026-01-01 2026-01-31 10
        symbols = sys.argv[1].split(",")
        start = sys.argv[2]
        end = sys.argv[3]
        concurrent = int(sys.argv[4]) if len(sys.argv) >= 5 else 10
        path = asyncio.run(run_pipeline(
            symbols=symbols,
            start_date=start,
            end_date=end,
            alpaca_key=os.getenv("ALPACA_API_KEY", ""),
            alpaca_secret=os.getenv("ALPACA_API_SECRET", ""),
            llm_endpoint=os.getenv("LLM_API_ENDPOINT", "https://api.openai.com/v1/chat/completions"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            max_concurrent=concurrent,
        ))
        print(f"\n输出文件: {path}")
    else:
        print("用法: python historical_news_pipeline.py <SYMBOLS> <START> <END> [CONCURRENT]")
        print("示例: python historical_news_pipeline.py TSLA,AAPL,NVDA 2026-01-01 2026-01-31 10")
        print()
        print("运行默认 demo...")
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\n中断。")
