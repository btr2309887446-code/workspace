"""
主事件循环与调度中心 (Pipeline)
===============================
系统的唯一入口，使用 asyncio.run() 启动全部并发组件。
通过事件驱动将各模块编排为完整的交易流水线：

  行情流 → Analytics(动能计算) → LLM Agent(风控裁决) → Order Manager(下单)
    ↑                                    │
    └── Session Manager(盘口门禁) ────────┘

特性：
- asyncio.create_task 并行运行所有组件
- 严密 try-except 包裹所有路径，单点异常不会导致系统崩溃
- SIGINT/SIGTERM 信号捕获，优雅关闭所有 WebSocket 和 HTTP 会话
"""

import asyncio
import logging
import signal
import sys
from typing import Dict, Optional

from alpaca.trading.client import TradingClient

from config import alpaca_cfg, strategy_cfg
from analytics import MultiSymbolAnalytics, MomentumSignal
from data_fetcher import DataFetcher
from llm_agent import LLMAgent, LLMDecision
from order_manager import OrderManager
from session_manager import SessionManager

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# 消费者协程：从行情队列消费数据、驱动动能计算、触发 LLM + 下单
# ---------------------------------------------------------------------------
async def consumer_loop(
    data_queue: asyncio.Queue,
    analytics: MultiSymbolAnalytics,
    llm_agent: LLMAgent,
    order_manager: OrderManager,
    session_manager: SessionManager,
    shutdown_event: asyncio.Event,
) -> None:
    """
    核心消费者循环。
    从 data_queue 中取出清洗后的行情数据，分发给对应标的的动能计算器。
    当收到异动信号时，调用 LLM 进行裁决，并执行交易。

    设计要点：
    - 使用 asyncio.wait_for 防止队列阻塞导致无法及时响应 shutdown。
    - 每轮循环先检查 shutdown_event，实现快速退出。
    - 所有异常在循环内部捕获，不会中断循环。
    """
    logger.info("消费者循环已启动")

    while not shutdown_event.is_set():
        # 从行情队列获取数据，带超时以响应 shutdown
        try:
            tick = await asyncio.wait_for(data_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue  # 超时后回到循环顶部检查 shutdown_event

        try:
            symbol = tick.get("symbol", "")
            if not symbol:
                continue

            # 仅处理 Trade 数据用于动能计算；Quote 可用于其他逻辑但此处跳过
            if tick.get("type") != "trade":
                continue

            price = float(tick["price"])
            volume = int(tick["volume"])
            ts = tick["timestamp"].timestamp() if tick.get("timestamp") else None

            # ---- Step 1: 喂入动能计算器 ----
            signal_ = analytics.feed(symbol, price, volume, ts)

            if signal_ is None:
                continue

            # ---- Step 2: 盘口门禁 ----
            if not session_manager.is_open:
                logger.debug("[%s] 盘口关闭，跳过下单", symbol)
                continue

            # ---- Step 3: 唤醒 LLM 风控裁判 ----
            decision = await llm_agent.judge(
                symbol=signal_.symbol,
                price=signal_.price,
                velocity=signal_.velocity,
                energy=signal_.energy,
                window_integral=signal_.window_energy_integral,
                avg_velocity=signal_.avg_velocity,
            )

            if decision is None:
                continue

            # ---- Step 4: 执行交易 ----
            if decision.action == "BUY":
                await order_manager.execute_buy(
                    symbol=symbol, price=signal_.price, confidence=decision.confidence
                )
            elif decision.action == "SELL":
                await order_manager.execute_sell(symbol=symbol)
            else:
                logger.debug("[%s] LLM 判定 HOLD，不执行操作", symbol)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("消费者循环内部异常（已捕获，继续运行）")

    logger.info("消费者循环已退出")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def main() -> None:
    """系统主调度中心。初始化所有组件并并发启动。"""

    # ------------------------------------------------------------------
    # 0. 初始化基础组件
    # ------------------------------------------------------------------
    trading_client = TradingClient(
        api_key=alpaca_cfg.API_KEY,
        secret_key=alpaca_cfg.API_SECRET,
        paper=True,
        url=alpaca_cfg.BASE_URL,
    )

    session_manager = SessionManager(trading_client, poll_interval=20.0)
    data_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    data_fetcher = DataFetcher(queue=data_queue)
    analytics = MultiSymbolAnalytics(tickers=strategy_cfg.TICKERS)
    llm_agent = LLMAgent()
    order_manager = OrderManager(trading_client)

    # 优雅关闭信号
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        """捕获 SIGINT (Ctrl+C) / SIGTERM，触发优雅关闭。"""
        logger.warning("收到终止信号，开始优雅关闭...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler，使用 signal.signal 兜底
            signal.signal(sig, lambda s, f: _signal_handler())

    # ------------------------------------------------------------------
    # 1. 启动盘口轮询
    # ------------------------------------------------------------------
    await session_manager.start()
    logger.info("盘口控制器已启动")

    # ------------------------------------------------------------------
    # 2. 并发启动行情源与消费者
    # ------------------------------------------------------------------
    tasks = []

    # 行情数据源任务
    feed_task = asyncio.create_task(data_fetcher.run(), name="data_fetcher")
    tasks.append(feed_task)

    # 消费者任务
    consumer_task = asyncio.create_task(
        consumer_loop(
            data_queue=data_queue,
            analytics=analytics,
            llm_agent=llm_agent,
            order_manager=order_manager,
            session_manager=session_manager,
            shutdown_event=shutdown_event,
        ),
        name="consumer",
    )
    tasks.append(consumer_task)

    logger.info("=" * 60)
    logger.info("🚀 微观动能追踪量化系统已就绪")
    logger.info("   标的池: %s", strategy_cfg.TICKERS)
    logger.info("   能量阈值: %.0f", strategy_cfg.ENERGY_THRESHOLD)
    logger.info("   单笔仓位上限: %.0f%%", strategy_cfg.MAX_POSITION_RATIO * 100)
    logger.info("   LLM 模型: %s", llm_agent._model)
    logger.info("   按 Ctrl+C 优雅退出")
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 3. 等待 shutdown 信号
    # ------------------------------------------------------------------
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("开始关闭所有组件...")

    # ------------------------------------------------------------------
    # 4. 优雅关闭
    # ------------------------------------------------------------------
    # 停止行情源
    await data_fetcher.stop()

    # 停止盘口轮询
    await session_manager.stop()

    # 取消所有任务
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # 关闭 LLM Agent 的 HTTP 会话
    await llm_agent.close()

    logger.info("所有组件已关闭，程序退出")


# ---------------------------------------------------------------------------
# 程序入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户手动中断 (KeyboardInterrupt)")
        sys.exit(0)
