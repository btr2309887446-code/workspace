"""
异步时序数据库模块 (database.py)
==================================
职责：
  1. 基于 aiosqlite 建立本地 SQLite 数据库，零阻塞主事件循环
  2. 维护 3 张核心表：ticks_history / llm_decisions / energy_alerts
  3. 异步批量刷写机制——内部缓冲队列 + 后台协程定时/定量触发事务写入
  4. 优雅停机时强制刷盘，确保零数据丢失

架构：
  pipeline → enqueue_tick / enqueue_llm / enqueue_alert  → 内部缓冲区
                                                              │
                ┌─────────────────────────────────────────────┘
                ▼
           _flush_loop() 后台协程
           (满 N 条 或 超时 3 秒 → executemany 事务写入)

依赖：
  - aiosqlite（异步 SQLite 驱动）
  - config.Settings（数据库路径等配置）
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, List, Tuple, Any

logger = logging.getLogger("SwapMomentum.Database")


# ============================================================================
# 异步数据库管理器
# ============================================================================

class AsyncDatabaseManager:
    """
    基于 aiosqlite 的异步时序数据库管理器。

    特性：
      - 自动建表（含联合索引）
      - 三队列批量缓冲（ticks / llm / alerts）
      - 后台 _flush_loop 协程批量刷写
      - shutdown → force_flush 保证零数据丢失
    """

    # 批量刷写触发阈值
    FLUSH_BATCH_SIZE = 50       # 单表缓冲区满 50 条即刷
    FLUSH_TIMEOUT_SEC = 3.0     # 距上次刷写超 3 秒即刷

    def __init__(self, db_path: str = "data/quant_memory.db"):
        """
        初始化数据库管理器。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None  # type: ignore

        # 三张表的写入缓冲区：[(timestamp, ticker, ...), ...]
        self._tick_buffer: List[Tuple] = []
        self._llm_buffer: List[Tuple] = []
        self._alert_buffer: List[Tuple] = []

        # 刷写锁——防止 _flush_loop 和 force_flush 并发写
        self._flush_lock = asyncio.Lock()

        # 上次刷写时间戳
        self._last_flush_ts: float = 0.0

        # 运行状态
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None

        # 统计
        self.stats = {
            "ticks_written": 0,
            "llm_written": 0,
            "alerts_written": 0,
            "flush_count": 0,
            "flush_errors": 0,
        }

    # ------------------------------------------------------------------
    # 公开方法：初始化 & 建表
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        """
        初始化数据库连接并自动建表。

        创建 3 张表，每张表在 (timestamp, ticker) 上建立联合索引，
        优化时序查询性能。
        """
        import aiosqlite

        # 确保目录存在
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)

        # WAL 模式：提升并发写入性能
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

        # ── 建表：高频瞬时状态 ──
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                ticker      TEXT    NOT NULL,
                price       REAL    NOT NULL,
                velocity    REAL    NOT NULL,
                energy      REAL    NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_ts_ticker
            ON ticks_history (timestamp, ticker)
        """)

        # ── 建表：LLM 风控决策日志 ──
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    REAL    NOT NULL,
                ticker       TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                confidence   REAL    NOT NULL,
                reasoning    TEXT,
                prompt_tokens INTEGER DEFAULT 0
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_ts_ticker
            ON llm_decisions (timestamp, ticker)
        """)

        # ── 建表：能量告警事件 ──
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS energy_alerts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      REAL    NOT NULL,
                ticker         TEXT    NOT NULL,
                current_energy REAL    NOT NULL,
                threshold      REAL    NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_ts_ticker
            ON energy_alerts (timestamp, ticker)
        """)

        await self._conn.commit()
        logger.info(f"数据库初始化完成 | path={self.db_path}")

    # ------------------------------------------------------------------
    # 公开方法：启动 & 停止
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        启动后台刷写协程。

        必须在 init_db() 之后、入队数据之前调用。
        """
        self._running = True
        self._last_flush_ts = time.time()
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="DB_FlushLoop"
        )
        logger.info("数据库后台刷写协程已启动")

    async def close(self) -> None:
        """
        优雅关闭——强制刷盘 + 关闭连接。

        执行顺序：
          1. 停止后台刷写协程
          2. 强制刷写所有缓冲区残余数据
          3. 关闭 SQLite 连接
        """
        logger.info("正在关闭数据库...")
        self._running = False

        # 取消后台刷写任务
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # 强制刷写残余数据
        await self._force_flush()

        # 关闭连接
        if self._conn:
            await self._conn.close()
            logger.info("数据库连接已关闭")

        logger.info(
            f"数据库已关闭 | 总写入={sum(self.stats.values()):,}"
        )

    # ------------------------------------------------------------------
    # 公开方法：数据入队（非阻塞）
    # ------------------------------------------------------------------

    def enqueue_tick(
        self, timestamp: float, ticker: str,
        price: float, velocity: float, energy: float,
    ) -> None:
        """
        将一条瞬时状态数据加入刷写缓冲区。

        Args:
            timestamp: UNIX 时间戳（秒）
            ticker:    合约代码
            price:     当前价格
            velocity:  EMA 速度
            energy:    瞬时能量
        """
        self._tick_buffer.append((timestamp, ticker, price, velocity, energy))

    def enqueue_llm_decision(
        self, timestamp: float, ticker: str,
        action: str, confidence: float,
        reasoning: str, prompt_tokens: int = 0,
    ) -> None:
        """
        将一条 LLM 风控决策加入刷写缓冲区。

        Args:
            timestamp:     UNIX 时间戳
            ticker:        合约代码
            action:        BUY / SELL / HOLD
            confidence:    置信度 0.0~1.0
            reasoning:     判断逻辑文本
            prompt_tokens: 消耗的 token 数（可选）
        """
        self._llm_buffer.append(
            (timestamp, ticker, action, confidence, reasoning, prompt_tokens)
        )

    def enqueue_alert(
        self, timestamp: float, ticker: str,
        current_energy: float, threshold: float,
    ) -> None:
        """
        将一条能量突破告警加入刷写缓冲区。

        Args:
            timestamp:      UNIX 时间戳
            ticker:         合约代码
            current_energy: 触发时的瞬时能量
            threshold:      触发阈值
        """
        self._alert_buffer.append(
            (timestamp, ticker, current_energy, threshold)
        )

    # ------------------------------------------------------------------
    # 内部：后台批量刷写循环
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """
        后台协程：周期性检测缓冲区并触发批量刷写。

        触发条件（满足任一即刷）：
          1. 任意缓冲区长度 >= FLUSH_BATCH_SIZE (50 条)
          2. 距上次刷写超过 FLUSH_TIMEOUT_SEC (3 秒) 且缓冲区非空
        """
        logger.debug("数据库刷写循环已启动")

        while self._running:
            try:
                await asyncio.sleep(0.5)  # 每 0.5 秒检查一次

                should_flush = False

                # 条件 1：单表缓冲区满
                if (
                    len(self._tick_buffer) >= self.FLUSH_BATCH_SIZE
                    or len(self._llm_buffer) >= self.FLUSH_BATCH_SIZE
                    or len(self._alert_buffer) >= self.FLUSH_BATCH_SIZE
                ):
                    should_flush = True

                # 条件 2：超时且缓冲区非空
                total_pending = (
                    len(self._tick_buffer)
                    + len(self._llm_buffer)
                    + len(self._alert_buffer)
                )
                elapsed = time.time() - self._last_flush_ts
                if total_pending > 0 and elapsed >= self.FLUSH_TIMEOUT_SEC:
                    should_flush = True

                if should_flush:
                    await self._do_flush()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("后台刷写循环异常")
                self.stats["flush_errors"] += 1

        logger.debug("数据库刷写循环已退出")

    async def _do_flush(self) -> None:
        """
        执行一次批量事务刷写。

        使用 executemany 将三个缓冲区的数据分别写入对应表，
        全部在一个事务中完成，确保原子性。

        刷写时加锁，防止与 force_flush 并发。
        """
        async with self._flush_lock:
            ticks = self._tick_buffer[:]
            llms = self._llm_buffer[:]
            alerts = self._alert_buffer[:]

            if not ticks and not llms and not alerts:
                return

            total = len(ticks) + len(llms) + len(alerts)

            try:
                async with self._conn.execute("BEGIN IMMEDIATE"):
                    if ticks:
                        await self._conn.executemany(
                            "INSERT INTO ticks_history (timestamp, ticker, price, velocity, energy) "
                            "VALUES (?, ?, ?, ?, ?)",
                            ticks,
                        )
                        self.stats["ticks_written"] += len(ticks)
                        self._tick_buffer = self._tick_buffer[len(ticks):]

                    if llms:
                        await self._conn.executemany(
                            "INSERT INTO llm_decisions (timestamp, ticker, action, confidence, reasoning, prompt_tokens) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            llms,
                        )
                        self.stats["llm_written"] += len(llms)
                        self._llm_buffer = self._llm_buffer[len(llms):]

                    if alerts:
                        await self._conn.executemany(
                            "INSERT INTO energy_alerts (timestamp, ticker, current_energy, threshold) "
                            "VALUES (?, ?, ?, ?)",
                            alerts,
                        )
                        self.stats["alerts_written"] += len(alerts)
                        self._alert_buffer = self._alert_buffer[len(alerts):]

                # 提交事务
                await self._conn.commit()

                self._last_flush_ts = time.time()
                self.stats["flush_count"] += 1

                logger.debug(
                    f"批量刷写完成 | ticks={len(ticks)} llms={len(llms)} "
                    f"alerts={len(alerts)} total={total}"
                )

            except Exception:
                logger.exception(
                    f"批量刷写事务失败 | pending={total}"
                )
                self.stats["flush_errors"] += 1
                # 失败时不清理缓冲区，下次重试

    async def _force_flush(self) -> None:
        """
        强制刷写所有缓冲区残余数据（用于优雅停机）。

        无论缓冲区大小，一次性全部写入。
        """
        async with self._flush_lock:
            ticks = self._tick_buffer[:]
            llms = self._llm_buffer[:]
            alerts = self._alert_buffer[:]

            total = len(ticks) + len(llms) + len(alerts)

            if total == 0:
                logger.debug("强制刷写：缓冲区为空，跳过")
                return

            try:
                async with self._conn.execute("BEGIN IMMEDIATE"):
                    if ticks:
                        await self._conn.executemany(
                            "INSERT INTO ticks_history (timestamp, ticker, price, velocity, energy) "
                            "VALUES (?, ?, ?, ?, ?)",
                            ticks,
                        )
                        self.stats["ticks_written"] += len(ticks)
                        self._tick_buffer.clear()

                    if llms:
                        await self._conn.executemany(
                            "INSERT INTO llm_decisions (timestamp, ticker, action, confidence, reasoning, prompt_tokens) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            llms,
                        )
                        self.stats["llm_written"] += len(llms)
                        self._llm_buffer.clear()

                    if alerts:
                        await self._conn.executemany(
                            "INSERT INTO energy_alerts (timestamp, ticker, current_energy, threshold) "
                            "VALUES (?, ?, ?, ?)",
                            alerts,
                        )
                        self.stats["alerts_written"] += len(alerts)
                        self._alert_buffer.clear()

                await self._conn.commit()
                logger.info(f"强制刷写完成（停机）| total={total}")

            except Exception:
                logger.exception(f"强制刷写失败（停机）| pending={total}")
                self.stats["flush_errors"] += 1

    # ------------------------------------------------------------------
    # 公开：查询接口（示例，可按需扩展）
    # ------------------------------------------------------------------

    async def query_recent_ticks(
        self, ticker: str, limit: int = 100,
    ) -> list:
        """
        查询某标的最近的瞬时状态记录。

        Args:
            ticker: 合约代码
            limit:  最大返回条数

        Returns:
            [(id, timestamp, ticker, price, velocity, energy), ...]
        """
        if not self._conn:
            return []
        cursor = await self._conn.execute(
            "SELECT id, timestamp, ticker, price, velocity, energy "
            "FROM ticks_history "
            "WHERE ticker = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (ticker, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def query_llm_decisions(
        self, ticker: str = None, limit: int = 50,
    ) -> list:
        """
        查询 LLM 风控决策日志。

        Args:
            ticker: 合约代码（None = 全部）
            limit:  最大返回条数

        Returns:
            [(id, timestamp, ticker, action, confidence, reasoning, prompt_tokens), ...]
        """
        if not self._conn:
            return []
        if ticker:
            cursor = await self._conn.execute(
                "SELECT * FROM llm_decisions "
                "WHERE ticker = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (ticker, limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM llm_decisions "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def get_table_counts(self) -> dict:
        """获取各表行数统计。"""
        if not self._conn:
            return {}
        tables = ["ticks_history", "llm_decisions", "energy_alerts"]
        counts = {}
        for table in tables:
            cursor = await self._conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            counts[table] = row[0] if row else 0
            await cursor.close()
        return counts
