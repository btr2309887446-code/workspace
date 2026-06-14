"""
OKX 历史数据下载与清洗工具 (okx_data_crawler.py)
=================================================
职责：
  1. 从 OKX 官方 CDN 异步下载逐笔成交 (Trades) 与极速买卖盘 (BBO) 历史数据
  2. 在内存中解压 ZIP，提取 CSV，按毫秒级时间戳进行前向填充合并
  3. 输出为 Parquet 格式，供 backtest_feeder.py 直接高速读取
  4. 集成 tqdm 进度条 + asyncio.Semaphore 并发控制 + 指数退避重试

数据流：
  OKX CDN → aiohttp 下载 → ZIP 解压 → pandas merge_asof
  → Parquet 写入 data/historical/{ticker}/

合并逻辑 (merge_asof)：
  Trades 时间戳 (ts) 为主键，BBO 数据向后填充 (direction="backward")。
  即：每笔成交匹配其发生时刻之前最近一次盘口快照（≤ 该成交时间戳）。
  这确保了不会用未来的盘口解释过去的成交 — 无 look-ahead bias。
"""

import asyncio
import io
import logging
import os
import random
import time
import zipfile
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, List, Tuple

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm as async_tqdm

logger = logging.getLogger("OKXCrawler")


# ============================================================================
# OKX CDN 端点常量
# ============================================================================

OKX_CDN_BASE = "https://www.okx.com/cdn/okex/traderecords/swap/daily"

# 文件名模板
TRADES_FILENAME = "{instId}-trades-{date_str}.zip"
BBO_FILENAME    = "{instId}-book-{date_str}.zip"


# ============================================================================
# 异步数据下载器
# ============================================================================

class OKXDataCrawler:
    """
    OKX 历史数据异步下载与清洗引擎。

    使用示例：
        crawler = OKXDataCrawler(
            ticker="TSLA-USDT-SWAP",
            start_date="2026-01-01",
            end_date="2026-01-31",
            output_dir="data/historical",
            max_concurrent=5,
        )
        await crawler.run()

    输出结构：
        data/historical/TSLA-USDT-SWAP/
            2026-01-01.parquet
            2026-01-02.parquet
            ...
    """

    def __init__(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        output_dir: str = "data/historical",
        max_concurrent: int = 5,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        timeout: float = 30.0,
    ):
        """
        初始化下载器。

        Args:
            ticker:         合约代码，如 "TSLA-USDT-SWAP"
            start_date:     起始日期 "YYYY-MM-DD"
            end_date:       截止日期 "YYYY-MM-DD"
            output_dir:     输出根目录
            max_concurrent: 最大并发下载数（防封 IP）
            max_retries:    单文件最大重试次数
            retry_delay:    重试基础延迟（秒）
            timeout:        HTTP 请求超时（秒）
        """
        self.ticker = ticker
        self.start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        self.end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        self.output_dir = Path(output_dir) / ticker
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout

        # 统计
        self.stats = {
            "total_days": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "merged": 0,
        }

        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开方法：主入口
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """
        执行完整的下载→解压→合并→存储流程。

        Returns:
            运行统计 dict
        """
        # 生成日期列表
        dates = self._generate_date_list()
        self.stats["total_days"] = len(dates)

        logger.info(
            f"开始下载 {self.ticker} 历史数据 | "
            f"{self.start_date} ~ {self.end_date} | "
            f"共 {len(dates)} 天 | 并发={self.max_concurrent}"
        )

        # 创建信号量控制并发
        semaphore = asyncio.Semaphore(self.max_concurrent)

        # 为每天创建下载任务
        tasks = []
        for d in dates:
            # 如果已存在 parquet 文件，跳过
            output_path = self._get_output_path(d)
            if output_path.exists():
                logger.info(f"  {d} 已存在，跳过")
                self.stats["skipped"] += 1
                continue
            tasks.append(self._process_day(semaphore, d))

        if not tasks:
            logger.info("所有数据已是最新，无下载任务")
            return self.stats

        # 并发执行，带 tqdm 进度条
        results = []
        for coro in async_tqdm.as_completed(
            tasks,
            desc=f"Downloading {self.ticker}",
            total=len(tasks),
            unit="day",
        ):
            try:
                result = await coro
                results.append(result)
            except Exception as e:
                logger.error(f"任务异常: {e}")
                self.stats["failed"] += 1

        # 汇总
        success_days = [r for r in results if r is not None]
        self.stats["downloaded"] = len(success_days)
        self.stats["merged"] = len(success_days)

        logger.info(
            f"下载完成 | total={self.stats['total_days']} "
            f"downloaded={self.stats['downloaded']} "
            f"skipped={self.stats['skipped']} "
            f"failed={self.stats['failed']}"
        )

        return self.stats

    # ------------------------------------------------------------------
    # 内部：单日处理
    # ------------------------------------------------------------------

    async def _process_day(
        self, semaphore: asyncio.Semaphore, day: date
    ) -> Optional[Path]:
        """
        处理单日数据：下载 → 解压 → 合并 → 保存。

        Args:
            semaphore: 并发控制信号量
            day:       目标日期

        Returns:
            输出文件路径，失败返回 None
        """
        async with semaphore:
            date_str = day.strftime("%Y-%m-%d")
            date_compact = day.strftime("%Y%m%d")

            # 构建下载 URL
            trades_url = self._build_url("trades", date_compact, date_str)
            bbo_url = self._build_url("book", date_compact, date_str)

            # 带重试的下载
            trades_zip = await self._download_with_retry(trades_url)
            if trades_zip is None:
                logger.warning(f"  {date_str} Trades 下载失败，跳过")
                self.stats["failed"] += 1
                return None

            bbo_zip = await self._download_with_retry(bbo_url)
            if bbo_zip is None:
                logger.warning(f"  {date_str} BBO 下载失败，跳过")
                self.stats["failed"] += 1
                return None

            # 在线程池中解压并合并（pandas 操作是同步的）
            try:
                output_path = await asyncio.to_thread(
                    self._extract_merge_save,
                    trades_zip, bbo_zip, date_str,
                )
                if output_path:
                    logger.info(f"  ✓ {date_str} → {output_path.name}")
                return output_path
            except Exception:
                logger.exception(f"  {date_str} 数据合并失败")
                self.stats["failed"] += 1
                return None

    # ------------------------------------------------------------------
    # 内部：URL 构建
    # ------------------------------------------------------------------

    def _build_url(self, data_type: str, date_compact: str, date_str: str) -> str:
        """
        构建 OKX CDN 下载 URL。

        URL 格式：
          https://www.okx.com/cdn/okex/traderecords/swap/daily/{YYYYMMDD}/{instId}-trades-{YYYY-MM-DD}.zip
          https://www.okx.com/cdn/okex/traderecords/swap/daily/{YYYYMMDD}/{instId}-book-{YYYY-MM-DD}.zip

        Args:
            data_type:    "trades" 或 "book"
            date_compact: "20260101"
            date_str:     "2026-01-01"
        """
        if data_type == "trades":
            filename = TRADES_FILENAME.format(
                instId=self.ticker, date_str=date_str
            )
        else:
            filename = BBO_FILENAME.format(
                instId=self.ticker, date_str=date_str
            )

        return f"{OKX_CDN_BASE}/{date_compact}/{filename}"

    # ------------------------------------------------------------------
    # 内部：异步下载 + 重试
    # ------------------------------------------------------------------

    async def _download_with_retry(self, url: str) -> Optional[bytes]:
        """
        带指数退避重试的异步文件下载。

        Args:
            url: 下载链接

        Returns:
            文件字节内容，全部重试失败返回 None
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; QuantBot/1.0)",
                        },
                    ) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        elif resp.status == 404:
                            # 该日无数据（合约可能尚未上线）
                            logger.debug(f"  404 Not Found: {url.split('/')[-1]}")
                            return None
                        else:
                            logger.warning(
                                f"  HTTP {resp.status} on {url.split('/')[-1]} "
                                f"(attempt {attempt}/{self.max_retries})"
                            )
            except asyncio.TimeoutError:
                logger.warning(
                    f"  超时: {url.split('/')[-1]} "
                    f"(attempt {attempt}/{self.max_retries})"
                )
            except aiohttp.ClientError as e:
                logger.warning(
                    f"  网络错误: {e} "
                    f"(attempt {attempt}/{self.max_retries})"
                )
            except Exception:
                logger.exception(f"  下载异常: {url.split('/')[-1]}")

            # 指数退避 + 随机抖动
            if attempt < self.max_retries:
                delay = self.retry_delay * (2 ** (attempt - 1))
                delay *= 0.5 + random.random()
                await asyncio.sleep(delay)

        return None

    # ------------------------------------------------------------------
    # 内部：解压 + 合并 + 保存（同步，在线程池中执行）
    # ------------------------------------------------------------------

    def _extract_merge_save(
        self, trades_zip: bytes, bbo_zip: bytes, date_str: str
    ) -> Optional[Path]:
        """
        解压 ZIP → 读取 CSV → merge_asof 对齐 → 保存 Parquet。

        合并逻辑 (merge_asof forward fill)：
          以 Trades 的 timestamp 为锚点，
          每条成交匹配 ≤ 其时间戳的最新 BBO 快照。
          这确保了不会用未来的订单簿信息解释过去的成交。

        Args:
            trades_zip: Trades ZIP 文件字节
            bbo_zip:    BBO ZIP 文件字节
            date_str:   日期字符串

        Returns:
            输出的 .parquet 文件路径
        """
        # ── 解压 Trades ──
        trades_df = self._read_csv_from_zip(
            trades_zip, "trades"
        )
        if trades_df is None or trades_df.empty:
            logger.warning(f"  {date_str} Trades 为空")
            return None

        # ── 解压 BBO ──
        bbo_df = self._read_csv_from_zip(
            bbo_zip, "book"
        )
        if bbo_df is None or bbo_df.empty:
            logger.warning(f"  {date_str} BBO 为空，仅保存 Trades")
            # 无 BBO 时仍保存 Trades（ask/bid 列为 NaN）
            merged = trades_df.copy()
            merged["ask"] = float("nan")
            merged["ask_sz"] = float("nan")
            merged["bid"] = float("nan")
            merged["bid_sz"] = float("nan")
        else:
            # ── 标准化列名 ──
            trades_df = self._normalize_trades_columns(trades_df)
            bbo_df = self._normalize_bbo_columns(bbo_df)

            # ── 时间戳统一为毫秒 ──
            if "ts" not in trades_df.columns or "ts" not in bbo_df.columns:
                logger.error(f"  {date_str} 缺少 ts 列"
                             f" trades_cols={list(trades_df.columns)}"
                             f" bbo_cols={list(bbo_df.columns)}")
                return None

            # 确保 ts 为整数类型
            trades_df["ts"] = trades_df["ts"].astype("int64")
            bbo_df["ts"] = bbo_df["ts"].astype("int64")

            # 按时间戳排序
            trades_df = trades_df.sort_values("ts").reset_index(drop=True)
            bbo_df = bbo_df.sort_values("ts").reset_index(drop=True)

            # ── merge_asof 后向填充（无 look-ahead bias） ──
            # direction="backward" 表示：
            #   对于 trades 中的每行 ts_t，匹配 bbo 中 ≤ ts_t 的最新行
            #   即 "用过去最近一次盘口快照解释当前成交"
            #   这确保了不会用未来的订单簿信息 — 无 look-ahead bias
            merged = pd.merge_asof(
                trades_df,
                bbo_df,
                on="ts",
                direction="backward",
                suffixes=("", "_bbo"),
            )

        # ── 重命名列以匹配回测系统 ──
        merged = merged.rename(columns={
            "px": "price",
            "sz": "volume",
            "ts": "timestamp",
        })

        # 确保必需的列存在
        for col in ("price", "volume", "ask", "bid"):
            if col not in merged.columns:
                merged[col] = float("nan")

        # 计算 volume_usdt
        if "price" in merged.columns and "volume" in merged.columns:
            merged["volume_usdt"] = merged["price"] * merged["volume"]
        else:
            merged["volume_usdt"] = 0.0

        # 添加 symbol 列
        merged["symbol"] = self.ticker

        # 选择回测系统需要的列
        output_cols = [
            "timestamp", "symbol", "price", "volume",
            "volume_usdt", "ask", "bid",
        ]
        # 可选列
        for col in ("ask_sz", "bid_sz"):
            if col in merged.columns:
                output_cols.append(col)

        available_cols = [c for c in output_cols if c in merged.columns]
        merged = merged[available_cols]

        # ── 保存 Parquet ──
        output_path = self._get_output_path(
            datetime.strptime(date_str, "%Y-%m-%d").date()
        )
        merged.to_parquet(output_path, index=False)

        logger.debug(
            f"  {date_str} merged={len(merged)} rows → {output_path.name}"
        )

        return output_path

    # ------------------------------------------------------------------
    # 内部：CSV 解析与列名标准化
    # ------------------------------------------------------------------

    @staticmethod
    def _read_csv_from_zip(zip_bytes: bytes, data_type: str) -> Optional[pd.DataFrame]:
        """
        从 ZIP 字节流中读取 CSV 文件。

        OKX CDN 的 ZIP 中通常包含一个与文件名同名的 CSV。
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                # 获取 ZIP 内的文件名列表
                names = zf.namelist()
                if not names:
                    return None

                # 优先找 CSV 文件
                csv_name = None
                for name in names:
                    if name.endswith(".csv"):
                        csv_name = name
                        break

                if csv_name is None:
                    # 某些情况下只有一个非 CSV 文件（如直接是数据）
                    csv_name = names[0]

                with zf.open(csv_name) as f:
                    df = pd.read_csv(
                        io.BytesIO(f.read()),
                        low_memory=False,
                    )
                return df
        except zipfile.BadZipFile:
            logger.error(f"  ZIP 文件损坏 ({data_type})")
        except Exception:
            logger.exception(f"  解压 CSV 异常 ({data_type})")

        return None

    @staticmethod
    def _normalize_trades_columns(df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化 Trades 数据列名。

        OKX CDN Trades CSV 列（可能因版本而异）：
          timestamp / ts  → 时间戳（毫秒）
          px / price      → 成交价
          sz / size / qty → 成交量
        """
        rename_map = {}
        for col in df.columns:
            col_lower = col.strip().lower()
            if col_lower in ("timestamp", "ts", "time"):
                rename_map[col] = "ts"
            elif col_lower in ("px", "price", "last"):
                rename_map[col] = "px"
            elif col_lower in ("sz", "size", "qty", "vol"):
                rename_map[col] = "sz"
            elif col_lower in ("side", "direction"):
                rename_map[col] = "side"

        if rename_map:
            df = df.rename(columns=rename_map)

        return df

    @staticmethod
    def _normalize_bbo_columns(df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化 BBO 数据列名。

        OKX CDN BBO CSV 列（常见格式）：
          ts / timestamp          → 时间戳（毫秒）
          asks[0][0] / ask_px     → 卖一价
          asks[0][1] / ask_sz     → 卖一量
          bids[0][0] / bid_px     → 买一价
          bids[0][1] / bid_sz     → 买一量

        注意：OKX CDN 的实际列名可能是带括号的字符串，
        如 "asks[0][0]"，需要做健壮映射。
        """
        rename_map = {}
        for col in df.columns:
            col_lower = col.strip().lower()
            # 时间戳
            if col_lower in ("timestamp", "ts", "time"):
                rename_map[col] = "ts"
            # 卖一价
            elif col_lower in ("asks[0][0]", "ask_px", "askpx", "best_ask"):
                rename_map[col] = "ask"
            # 卖一量
            elif col_lower in ("asks[0][1]", "ask_sz", "asksz"):
                rename_map[col] = "ask_sz"
            # 买一价
            elif col_lower in ("bids[0][0]", "bid_px", "bidpx", "best_bid"):
                rename_map[col] = "bid"
            # 买一量
            elif col_lower in ("bids[0][1]", "bid_sz", "bidsz"):
                rename_map[col] = "bid_sz"

        if rename_map:
            df = df.rename(columns=rename_map)

        return df

    # ------------------------------------------------------------------
    # 内部：辅助方法
    # ------------------------------------------------------------------

    def _generate_date_list(self) -> List[date]:
        """生成日期列表 [start_date, ..., end_date]."""
        dates = []
        current = self.start_date
        while current <= self.end_date:
            dates.append(current)
            current += timedelta(days=1)
        return dates

    def _get_output_path(self, day: date) -> Path:
        """获取单日数据输出路径。"""
        filename = f"{day.strftime('%Y-%m-%d')}.parquet"
        return self.output_dir / filename


# ============================================================================
# 入口代码
# ============================================================================

async def main():
    """
    一键下载过去 30 天 TSLA-USDT-SWAP 的完整微观结构数据。

    merge_asof 对齐逻辑说明：
    ─────────────────────────
    Trades 数据每条记录代表一笔真实成交，时间戳精确到毫秒。
    BBO 数据每条记录代表订单簿的快照（通常间隔 10-100ms）。

    合并策略（前向填充 forward fill）：
      对于时刻 T 的一笔成交，匹配"最近一次 ≤ T"的盘口快照。
      
      示例时间轴：
        09:30:00.100  BBO: ask=248.50 bid=248.48
        09:30:00.150  TRADE: price=248.50 size=0.5
        09:30:00.200  BBO: ask=248.52 bid=248.50
        09:30:00.250  TRADE: price=248.52 size=0.3
      
      合并后：
        09:30:00.150  248.50  0.5  ask=248.50  bid=248.48  ← 用前一个BBO
        09:30:00.250  248.52  0.3  ask=248.52  bid=248.50  ← 用前一个BBO

    为什么不用 backward fill？
      backward fill 会用"未来"的盘口解释"过去"的成交，
      这在回测中引入 look-ahead bias，导致过拟合。
    """
    # 计算过去 30 天
    end = date.today()
    start = end - timedelta(days=30)

    crawler = OKXDataCrawler(
        ticker="TSLA-USDT-SWAP",
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        output_dir="data/historical",
        max_concurrent=5,
        max_retries=3,
    )

    stats = await crawler.run()
    print(f"\n下载统计: {stats}")
    print(f"输出目录: {crawler.output_dir}")


if __name__ == "__main__":
    import sys

    # 支持命令行参数
    if len(sys.argv) >= 4:
        ticker = sys.argv[1]
        start = sys.argv[2]
        end = sys.argv[3]
        output = sys.argv[4] if len(sys.argv) >= 5 else "data/historical"

        async def _cli():
            c = OKXDataCrawler(
                ticker=ticker,
                start_date=start,
                end_date=end,
                output_dir=output,
            )
            await c.run()

        asyncio.run(_cli())
    else:
        print("用法: python okx_data_crawler.py <TICKER> <START> <END> [OUTPUT_DIR]")
        print("示例: python okx_data_crawler.py TSLA-USDT-SWAP 2026-01-01 2026-01-31")
        print()
        print("运行默认 demo（过去 30 天 TSLA-USDT-SWAP）...")
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\n用户中断。")
        except Exception:
            import traceback
            traceback.print_exc()
