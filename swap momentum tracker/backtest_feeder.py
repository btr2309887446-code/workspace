"""
双轨时间线回测数据源 (backtest_feeder.py)
==========================================
职责：
  1. 同时读取微观 Tick 数据（CSV/Parquet）与宏观新闻情绪数据（Parquet）
  2. 按时间戳严格排序交织，模拟真实市场"价格+信息"双流到达
  3. Tick 事件 → 推入 data_queue 进行动能计算
  4. 新闻事件 → 直接更新 MacroSentimentPool，供 LLM 风控读取
  5. 对外接口 (start/stop) 与 SyntheticEquityFetcher 完全兼容

双流合并算法：
  Tick 流 + 新闻流 → pandas.concat → sort_values("timestamp")
  → 逐行迭代 → 根据 event_type 分发到不同处理路径

时间模拟：
  playback_speed=0 → 光速
  playback_speed>0 → await sleep(Δt / speed)
"""

import asyncio
import csv
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

import pandas as pd

from config import Settings

logger = logging.getLogger("SwapMomentum.BacktestFeeder")


# ============================================================================
# 双轨时间线回放器
# ============================================================================

class HistoricalCSVFeeder:
    """
    双轨时间线回放器 —— 接口完全兼容 SyntheticEquityFetcher。

    使用方法：
        from news_engine import MacroSentimentPool

        pool = MacroSentimentPool()

        feeder = HistoricalCSVFeeder(
            settings=settings,
            data_queue=data_queue,
            tick_path="data/historical/TSLA-USDT-SWAP/2026-01-01.parquet",
            news_path="data/historical/news_sentiment.parquet",
            sentiment_pool=pool,
            playback_speed=10.0,
        )
    """

    def __init__(
        self,
        settings: Settings,
        data_queue: asyncio.Queue,
        tick_path: str,
        news_path: str = None,
        sentiment_pool=None,  # MacroSentimentPool
        playback_speed: float = 1.0,
    ):
        """
        Args:
            settings:       系统配置
            data_queue:     异步队列（Tick 事件推入此队列）
            tick_path:      微观 Tick 数据文件（.csv 或 .parquet）
            news_path:      宏观新闻情绪文件（.parquet，可选）
            sentiment_pool: MacroSentimentPool 实例（新闻事件直接更新）
            playback_speed: 播放速率（0=光速, 1=实时, N=N倍速）
        """
        self.settings = settings
        self.data_queue = data_queue
        self.tick_path = Path(tick_path)
        self.news_path = Path(news_path) if news_path else None
        self.sentiment_pool = sentiment_pool
        self.playback_speed = playback_speed

        self._running = False
        self._current_symbols: Set[str] = set()
        self._messages_pushed = 0
        self._news_events = 0
        self._start_time = 0.0

        # 兼容旧参数名
        self.csv_path = self.tick_path

        # 统计
        self.stats: Dict[str, Any] = {
            "messages_received": 0,
            "messages_parsed": 0,
            "messages_dropped": 0,
            "news_events": 0,
            "tick_events": 0,
            "reconnect_attempts": 0,
            "subscription_changes": 0,
            "last_price": {},
            "last_update": {},
            "total_events": 0,
        }

    # ------------------------------------------------------------------
    # 公开接口（与 SyntheticEquityFetcher 一致）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        启动双轨时间线回放。

        流程：
          1. 读取 Tick 数据 → 标准化 → 打上 event_type="tick"
          2. 读取新闻数据 → 标准化 → 打上 event_type="news"
          3. 合并 → 按 timestamp 排序
          4. 逐行迭代，按事件类型分发
        """
        self._running = True
        self._start_time = time.time()

        if not self.tick_path.exists():
            logger.error(f"Tick 文件不存在: {self.tick_path}")
            self._running = False
            return

        has_news = self.news_path and self.news_path.exists()

        logger.info(
            f"双轨回放器启动 | tick={self.tick_path.name} | "
            f"news={self.news_path.name if has_news else '无'} | "
            f"speed={self.playback_speed}x"
        )

        try:
            await self._playback_loop()
        except asyncio.CancelledError:
            logger.info("回放协程被取消")
        except Exception:
            logger.exception("回放过程中发生异常")
        finally:
            self._running = False
            # 注入毒丸
            try:
                self.data_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

            elapsed = time.time() - self._start_time
            logger.info(
                f"双轨回放结束 | ticks={self._messages_pushed} "
                f"news={self._news_events} | 耗时={elapsed:.1f}s"
            )

    async def stop(self) -> None:
        self._running = False

    async def update_subscriptions(self, active_symbols: List[str]) -> None:
        self._current_symbols = set(active_symbols)
        self.stats["subscription_changes"] += 1

    # ------------------------------------------------------------------
    # 内部：双轨回放主循环
    # ------------------------------------------------------------------

    async def _playback_loop(self) -> None:
        """
        双轨时间线归并 + 回放。

        算法：
          1. 在线程池中读取 Tick + News 数据
          2. pandas.concat + sort_values("timestamp") 归并
          3. 逐行迭代，根据 event_type 分发：
             - "tick" → 推入 data_queue
             - "news" → 更新 sentiment_pool
          4. playback_speed 控制时间流逝
        """
        # 在线程池中加载并合并
        timeline = await asyncio.to_thread(self._build_timeline)
        self.stats["total_events"] = len(timeline)

        tick_count = sum(1 for e in timeline if e.get("event_type") == "tick")
        news_count = len(timeline) - tick_count
        self.stats["tick_events"] = tick_count
        self.stats["news_events"] = news_count

        logger.info(
            f"时间线构建完成 | total={len(timeline)} "
            f"ticks={tick_count} news={news_count}"
        )

        previous_ts: Optional[float] = None

        for event in timeline:
            if not self._running:
                break

            ts = float(event.get("timestamp", 0))
            etype = event.get("event_type", "tick")

            # ── 时间模拟 ──
            if self.playback_speed > 0 and previous_ts is not None:
                dt = ts - previous_ts
                if dt > 0:
                    wait = min(dt / self.playback_speed, 10.0)
                    await asyncio.sleep(wait)

            previous_ts = ts

            # ── 事件分发 ──
            if etype == "news":
                self._dispatch_news(event)
            else:
                self._dispatch_tick(event)

    # ------------------------------------------------------------------
    # 内部：时间线构建（同步，在线程池中执行）
    # ------------------------------------------------------------------

    def _build_timeline(self) -> List[Dict[str, Any]]:
        """
        构建统一的排序时间线。

        1. 读取 Tick 数据 → 标准化 → 添加 event_type="tick"
        2. 读取 News 数据 → 标准化 → 添加 event_type="news"
        3. pandas.concat → sort_values("timestamp") → records

        Returns:
            [{"timestamp": ..., "event_type": "tick"/"news", ...}, ...]
        """
        events = []

        # ── Tick 流 ──
        tick_rows = self._load_tick_data()
        for row in tick_rows:
            parsed = self._parse_row(row)
            if parsed is not None:
                parsed["event_type"] = "tick"
                # 确保顶层有 timestamp 键（_parse_row 返回 local_timestamp）
                if "timestamp" not in parsed:
                    parsed["timestamp"] = parsed.get(
                        "local_timestamp", parsed.get("server_timestamp", 0)
                    )
                events.append(parsed)

        # ── News 流 ──
        if self.news_path and self.news_path.exists():
            news_df = pd.read_parquet(self.news_path)
            for _, row in news_df.iterrows():
                ticker = str(row.get("ticker", ""))
                impact = float(row.get("impact", 0) or row.get("impact_score", 0))
                headline = str(row.get("headline", ""))
                # 时间戳统一为 float UNIX 秒
                ts_raw = row.get("timestamp", 0)
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                    elif isinstance(ts_raw, pd.Timestamp):
                        ts = ts_raw.timestamp()
                    else:
                        ts = float(ts_raw)
                except (ValueError, TypeError):
                    ts = 0.0

                if ticker and ts > 0 and abs(impact) > 0.001:
                    events.append({
                        "event_type": "news",
                        "timestamp": ts,
                        "ticker": ticker,
                        "impact_score": impact,
                        "headline": headline,
                        "symbol": ticker,  # 兼容
                    })

        if not events:
            return []

        # pandas 排序（毫秒级稳定排序）
        df = pd.DataFrame(events)
        df["timestamp"] = df["timestamp"].astype(float)
        df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
        return df.to_dict("records")

    # ------------------------------------------------------------------
    # 内部：事件分发
    # ------------------------------------------------------------------

    def _dispatch_tick(self, parsed: Dict[str, Any]) -> None:
        """Tick 事件 → 推入队列，走原有动能计算链路。"""
        try:
            self.data_queue.put_nowait(parsed)
            self._messages_pushed += 1
            self.stats["messages_received"] += 1
            self.stats["messages_parsed"] += 1
            symbol = parsed.get("symbol", "")
            if symbol:
                self.stats["last_price"][symbol] = parsed["price"]
                self.stats["last_update"][symbol] = parsed["local_timestamp"]
        except asyncio.QueueFull:
            self.stats["messages_dropped"] += 1

    def _dispatch_news(self, event: Dict[str, Any]) -> None:
        """
        新闻事件 → 直接更新 MacroSentimentPool。

        不推入 data_queue，因为队列中的消费者只处理价格数据。
        情绪池更新后，同时间或稍后的 Tick 触发 LLM 时，
        llm_agent 通过 get_current_bias() 即可读取最新情绪。
        """
        if self.sentiment_pool is None:
            return

        ticker = event.get("ticker", "")
        impact = event.get("impact_score", 0.0)
        headline = event.get("headline", "")

        self.sentiment_pool.update(
            ticker=ticker,
            impact_score=impact,
            headline=headline,
        )
        self._news_events += 1

        logger.debug(
            f"[BT-NEWS] {ticker} impact={impact:+.2f} | {headline[:50]}"
        )

    # ------------------------------------------------------------------
    # 内部：数据加载
    # ------------------------------------------------------------------

    def _load_tick_data(self) -> List[Dict[str, str]]:
        """根据文件扩展名加载 Tick 数据。"""
        ext = self.tick_path.suffix.lower()
        if ext == ".parquet":
            return self._read_parquet_tick()
        else:
            return self._read_csv_tick()

    def _read_csv_tick(self) -> List[Dict[str, str]]:
        rows = []
        with open(self.tick_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def _read_parquet_tick(self) -> List[Dict[str, str]]:
        """Parquet → 列名标准化 → 字典列表。"""
        df = pd.read_parquet(self.tick_path)
        rename = {}
        for col in df.columns:
            lc = str(col).lower().strip()
            if lc in ("ts", "time"):
                rename[col] = "timestamp"
            elif lc in ("ticker", "instid"):
                rename[col] = "symbol"
            elif lc in ("px",):
                rename[col] = "price"
            elif lc in ("sz", "qty"):
                rename[col] = "volume"
        if rename:
            df = df.rename(columns=rename)
        if "volume_usdt" not in df.columns:
            if "price" in df.columns and "volume" in df.columns:
                df["volume_usdt"] = df["price"] * df["volume"]
            else:
                df["volume_usdt"] = 0.0
        return df.where(pd.notna(df), None).to_dict("records")

    # ------------------------------------------------------------------
    # 内部：Tick 行解析
    # ------------------------------------------------------------------

    def _parse_row(self, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """将一行原始数据解析为标准化的行情 dict。"""
        lower_row = {k.lower().strip(): v for k, v in row.items()}

        try:
            ts_raw = lower_row.get("timestamp") or lower_row.get("time") or "0"
            try:
                timestamp = float(ts_raw)
            except ValueError:
                try:
                    dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    timestamp = dt.timestamp()
                except Exception:
                    timestamp = time.time()

            symbol = (
                lower_row.get("symbol")
                or lower_row.get("ticker")
                or lower_row.get("instid")
                or ""
            )
            if not symbol:
                return None

            price = float(
                lower_row.get("price")
                or lower_row.get("last")
                or lower_row.get("close")
                or 0
            )
            if price <= 0:
                return None

            volume_usdt = float(lower_row.get("volume_usdt", 0) or 0)
            last_trade_size = float(lower_row.get("last_trade_size", 0) or 0)

            if volume_usdt <= 0:
                raw_vol = float(
                    lower_row.get("volume")
                    or lower_row.get("lastsz")
                    or lower_row.get("vol")
                    or 0
                )
                volume_usdt = raw_vol * price
                last_trade_size = raw_vol if last_trade_size <= 0 else last_trade_size

            ask = float(lower_row.get("ask", 0) or 0)
            bid = float(lower_row.get("bid", 0) or 0)

            return {
                "symbol": symbol,
                "price": price,
                "last_trade_size": last_trade_size,
                "volume_usdt": volume_usdt,
                "ask": ask,
                "bid": bid,
                "spread": ask - bid if ask > 0 and bid > 0 else 0.0,
                "server_timestamp": timestamp,
                "local_timestamp": timestamp,
            }
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"行解析失败: {e} | row={str(row)[:200]}")
            return None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @property
    def progress_pct(self) -> float:
        total = self.stats.get("total_events", 0)
        if total == 0:
            return 0.0
        done = self._messages_pushed + self._news_events
        return min(100.0, done / total * 100.0)

    def __repr__(self) -> str:
        return (
            f"HistoricalCSVFeeder(file={self.tick_path.name}, "
            f"speed={self.playback_speed}x, "
            f"progress={self.progress_pct:.1f}%)"
        )


# 别名
HistoricalDataFeeder = HistoricalCSVFeeder
