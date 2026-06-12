"""
沙盒回测启动器 (run_backtest.py)
=================================
职责：
  1. 独立的回测入口——读取历史 CSV 数据，运行完整的计算 + LLM 分析管线
  2. 依赖注入：用 HistoricalCSVFeeder 替换实盘的 SyntheticEquityFetcher
  3. 状态机穿透：跳过 session_manager 休市拦截逻辑，
     确保 CSV 中每一行数据都被连续计算
  4. 支持 Mock LLM 模式，零 API 成本验证策略逻辑
  5. 可选接入 AsyncDatabaseManager，将回测结果持久化

运行方式：
  python run_backtest.py data/sample.csv --speed 10 --mock-llm

参数说明：
  csv_path        CSV 文件路径（位置参数）
  --speed / -s    回放速度倍数（默认 1 = 实时，0 = 光速）
  --mock-llm      启用 Mock LLM（不调用真实 API，始终返回 HOLD）
  --db / -d       启用数据库持久化（写入 data/quant_memory.db）
  --no-print      静默模式（不打印实时行情）
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from config import get_settings, setup_logging, Ansi
from analytics import MarketDynamicsCalculator
from llm_agent import MarketLLMAgent
from backtest_feeder import HistoricalCSVFeeder
from reporter import print_energy_alert, print_llm_decision

logger: Optional[logging.Logger] = None


# ============================================================================
# Mock LLM Agent —— 零 API 成本的假 LLM
# ============================================================================

class MockLLMAgent:
    """
    模拟 LLM 风控裁判——用于回测沙盒环境。

    始终返回 HOLD 决策，零 API 调用成本。
    接口与 MarketLLMAgent 完全一致，可直接依赖注入替换。
    """

    async def analyze(self, **kwargs) -> Optional[dict]:
        """始终返回 HOLD 的 mock 决策。"""
        return {
            "action": "HOLD",
            "confidence": 0.5,
            "reasoning": "[Mock LLM] 回测沙盒模式，未调用真实 API",
            "ticker": kwargs.get("ticker", "???"),
            "timestamp": time.time(),
            "raw_response": "mock",
            "price": kwargs.get("current_price", 0),
            "velocity": kwargs.get("velocity", 0),
            "energy": kwargs.get("energy", 0),
        }

    def get_stats(self) -> dict:
        return {
            "total_calls": 0,
            "success": 0,
            "failure": 0,
            "timeout": 0,
            "parse_fail": 0,
            "skipped_cooldown": 0,
            "api_format": "mock",
            "cooldown_active": {},
        }


# ============================================================================
# 回测消费者协程（跳过盘口过滤）
# ============================================================================

async def backtest_consumer(
    data_queue: asyncio.Queue,
    calculators: Dict[str, MarketDynamicsCalculator],
    llm_agent,
    settings,
    alert_counters: Dict[str, int],
    db_manager=None,  # type: ignore
    shutdown_event: asyncio.Event = None,
    quiet: bool = False,
) -> None:
    """
    回测消费者协程——与实盘 consumer_task 逻辑一致，但跳过盘口过滤。

    核心差异：
      - 不调用 session_manager.current_session()
      - CSV 中所有数据行无条件进入计算管线
      - 可选写入数据库

    Args:
        data_queue:     异步行情队列
        calculators:    {symbol: MarketDynamicsCalculator}
        llm_agent:      MarketLLMAgent 或 MockLLMAgent
        settings:       Settings 配置
        alert_counters: 告警计数
        db_manager:     可选数据库管理器
        shutdown_event: 停机信号
        quiet:          静默模式（不打印实时数据）
    """
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    last_print_time = 0.0
    header_printed = False
    background_tasks: set = set()

    logger.info("回测消费者协程已启动（跳过盘口过滤）")

    while not shutdown_event.is_set():
        try:
            data = await asyncio.wait_for(data_queue.get(), timeout=2.0)
        except asyncio.TimeoutError:
            # CSV 回放完毕，队列可能已空
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("回测消费者队列读取异常")
            continue

        if data is None:
            logger.info("回测消费者收到毒丸信号")
            break

        symbol = data.get("symbol", "")
        if not symbol:
            continue

        # ── 获取/创建计算器 ──
        if symbol not in calculators:
            calculators[symbol] = MarketDynamicsCalculator(settings)
            logger.info(f"回测: 为 {symbol} 创建计算器")

        calc = calculators[symbol]

        # ── 更新计算器 ──
        try:
            timestamp = (
                data.get("server_timestamp")
                or data.get("local_timestamp", time.time())
            )
            result = calc.update(
                price=data["price"],
                volume_usdt=data["volume_usdt"],
                timestamp=timestamp,
            )
        except Exception:
            logger.exception(f"回测: {symbol} 计算器更新失败")
            continue

        # ── 数据库：写入 ticks_history ──
        if db_manager and result["initialized"]:
            db_manager.enqueue_tick(
                timestamp=result["timestamp"],
                ticker=symbol,
                price=result["price"],
                velocity=result["velocity"],
                energy=result["energy"],
            )

        # ── 能量告警 + LLM 触发 ──
        if result["initialized"] and result["energy"] > settings.energy_threshold:
            if symbol not in alert_counters:
                alert_counters[symbol] = 0

            now = time.time()
            last_alert = getattr(calc, "_last_alert_ts", 0)
            if now - last_alert >= settings.alert_cooldown_seconds:
                alert_counters[symbol] += 1
                calc._last_alert_ts = now  # type: ignore

                if not quiet:
                    print_energy_alert(
                        symbol=symbol,
                        price=result["price"],
                        velocity=result["velocity"],
                        energy=result["energy"],
                        threshold=settings.energy_threshold,
                        alert_count=alert_counters[symbol],
                        timestamp=result["timestamp"],
                    )

                # ── 数据库：写入能量告警 ──
                if db_manager:
                    db_manager.enqueue_alert(
                        timestamp=result["timestamp"],
                        ticker=symbol,
                        current_energy=result["energy"],
                        threshold=settings.energy_threshold,
                    )

                # ── 非阻塞触发 LLM ──
                now_ts = result["timestamp"]
                five_min_stats = calc.get_window_stats(now_ts - 300)

                llm_task = asyncio.create_task(
                    _backtest_llm_analyze(
                        llm_agent=llm_agent,
                        ticker=symbol,
                        current_price=result["price"],
                        velocity=result["velocity"],
                        energy=result["energy"],
                        five_min_stats=five_min_stats,
                        db_manager=db_manager,
                        quiet=quiet,
                    ),
                    name=f"bt_llm_{symbol}",
                )
                background_tasks.add(llm_task)
                llm_task.add_done_callback(background_tasks.discard)

                logger.warning(
                    f"回测能量告警 | {symbol} | price={result['price']:,.4f} | "
                    f"energy={result['energy']:,.4f}"
                )

        # ── 节流打印 ──
        if not quiet:
            now = time.time()
            if now - last_print_time >= 0.5:
                if result["initialized"]:
                    parts = []
                    for sym, c in sorted(calculators.items()):
                        if c.is_initialized:
                            d = "+" if c.velocity > 0.0001 else "-" if c.velocity < -0.0001 else "~"
                            parts.append(f"{sym.split('-')[0]}:{c.price:,.4f} {d}{abs(c.velocity):.4f}")
                    if parts:
                        print(f"  [BT] {' | '.join(parts)}", flush=True)
                last_print_time = now

            if not header_printed and result["initialized"]:
                print(f"\n{'─' * 78}")
                print(f"  {'Contract':<22} {'Price':>12} {'Velocity':>14} {'Energy':>14}")
                print(f"{'─' * 78}")
                header_printed = True

    logger.info("回测消费者协程已退出")


async def _backtest_llm_analyze(
    llm_agent,
    ticker: str,
    current_price: float,
    velocity: float,
    energy: float,
    five_min_stats: dict,
    db_manager=None,
    quiet: bool = False,
) -> None:
    """回测版 LLM 分析协程——含数据库写入。"""
    try:
        result = await llm_agent.analyze(
            ticker=ticker,
            current_price=current_price,
            velocity=velocity,
            energy=energy,
            five_min_stats=five_min_stats,
        )
        if result:
            if not quiet:
                print_llm_decision(result)

            # ── 数据库：写入 LLM 决策 ──
            if db_manager:
                db_manager.enqueue_llm_decision(
                    timestamp=result.get("timestamp", time.time()),
                    ticker=result.get("ticker", ticker),
                    action=result.get("action", "HOLD"),
                    confidence=result.get("confidence", 0.5),
                    reasoning=result.get("reasoning", ""),
                )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(f"回测 LLM 分析异常 | ticker={ticker}")


# ============================================================================
# 主入口
# ============================================================================

async def run_backtest(
    csv_path: str,
    playback_speed: float = 1.0,
    use_mock_llm: bool = False,
    use_db: bool = False,
    quiet: bool = False,
) -> None:
    """
    运行一次完整的回测。

    Args:
        csv_path:       CSV 数据文件路径
        playback_speed: 回放速率（0=光速，1=实时，N=N倍速）
        use_mock_llm:   是否使用 Mock LLM
        use_db:         是否启用数据库持久化
        quiet:          静默模式
    """
    global logger

    # 1. 配置
    settings = get_settings()
    setup_logging(settings)
    logger = logging.getLogger("SwapMomentum.Backtest")

    # ── 打印横幅 ──
    print(f"\n{Ansi.CYAN}{'=' * 64}")
    print(f"  BACKTEST SANDBOX")
    print(f"{'=' * 64}")
    print(f"  CSV          : {csv_path}")
    print(f"  Playback     : {playback_speed}x ({'光速' if playback_speed == 0 else f'{playback_speed}x'})")
    print(f"  Mock LLM     : {'启用' if use_mock_llm else '真实 API'}")
    print(f"  Database     : {'启用' if use_db else '禁用'}")
    print(f"  Energy Thr   : {settings.energy_threshold:,.0f}")
    print(f"{'=' * 64}{Ansi.RESET}\n")

    # 2. 实例化组件
    data_queue: asyncio.Queue = asyncio.Queue(maxsize=8192)

    # Feeder（依赖注入——用 HistoricalCSVFeeder 替换实盘 Fetcher）
    feeder = HistoricalCSVFeeder(
        settings=settings,
        data_queue=data_queue,
        csv_path=csv_path,
        playback_speed=playback_speed,
    )

    # LLM Agent
    if use_mock_llm:
        llm_agent = MockLLMAgent()
        logger.info("Mock LLM Agent 已启用（零 API 成本）")
    else:
        llm_agent = MarketLLMAgent(
            api_endpoint=settings.llm_api_endpoint,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            cooldown_seconds=settings.llm_cooldown_seconds,
        )

    # 数据库（可选）
    db_manager = None
    if use_db:
        from database import AsyncDatabaseManager
        db_manager = AsyncDatabaseManager()
        await db_manager.init_db()
        await db_manager.start()
        logger.info("数据库管理器已启动")

    # 计算器 & 告警计数
    calculators: Dict[str, MarketDynamicsCalculator] = {}
    alert_counters: Dict[str, int] = {}

    # 3. 停机信号
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler(sig, frame):
        logger.info(f"收到信号 signal={sig}")
        loop.call_soon_threadsafe(shutdown_event.set)
    signal.signal(signal.SIGINT, _signal_handler)

    # 4. 启动回测
    start_time = time.time()

    feeder_task = asyncio.create_task(feeder.start(), name="BacktestFeeder")
    consumer = asyncio.create_task(
        backtest_consumer(
            data_queue, calculators, llm_agent, settings,
            alert_counters, db_manager, shutdown_event, quiet,
        ),
        name="BacktestConsumer",
    )

    # 等待 feeder 完成（CSV 读完自动退出）
    try:
        await feeder_task
    except asyncio.CancelledError:
        pass

    # 短暂等待 consumer 消费完队列残余
    await asyncio.sleep(0.5)

    # 发送毒丸
    try:
        data_queue.put_nowait(None)
    except asyncio.QueueFull:
        pass

    # 等待 consumer 退出
    try:
        await asyncio.wait_for(consumer, timeout=10.0)
    except asyncio.TimeoutError:
        consumer.cancel()

    # 5. 关闭数据库
    if db_manager:
        await db_manager.close()
        counts = await db_manager.get_table_counts()
        logger.info(f"数据库统计: {counts}")

    elapsed = time.time() - start_time

    # 6. 打印回测摘要
    print(f"\n{Ansi.GREEN}{'=' * 64}")
    print(f"  BACKTEST COMPLETED")
    print(f"{'=' * 64}")
    total_samples = sum(c.sample_count for c in calculators.values())
    total_alerts = sum(alert_counters.values())
    print(f"  耗时          : {elapsed:.1f}s")
    print(f"  活跃标的      : {list(calculators.keys())}")
    print(f"  总采样点      : {total_samples:,}")
    print(f"  能量告警      : {total_alerts}")
    for sym, calc in sorted(calculators.items()):
        if calc.is_initialized:
            vs = calc.get_velocity_stats()
            es = calc.get_energy_stats()
            print(f"  {sym:<20} samples={calc.sample_count:>6,}  "
                  f"last_price={calc.price:>10,.4f}  "
                  f"vel_mean={vs['mean']:>+.6f}  "
                  f"energy_max={es['max']:>10.2f}")
    print(f"{'=' * 64}{Ansi.RESET}\n")


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="股权代币永续合约动量回测沙盒",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_backtest.py data/BTC_USDT.csv --speed 10 --mock-llm
  python run_backtest.py data/TSLA.csv -s 0 --db -q
        """,
    )
    parser.add_argument("csv_path", help="历史 CSV 数据文件路径")
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0,
        help="回放速率倍数（0=光速, 1=实时, 10=10倍速）",
    )
    parser.add_argument(
        "--mock-llm", action="store_true",
        help="启用 Mock LLM（不调用真实 API，节省成本）",
    )
    parser.add_argument(
        "-d", "--db", action="store_true",
        help="启用数据库持久化（写入 data/quant_memory.db）",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="静默模式（不打印实时行情，仅输出摘要）",
    )

    args = parser.parse_args()

    # 校验 CSV 文件存在
    if not Path(args.csv_path).exists():
        print(f"Error: CSV file not found: {args.csv_path}")
        sys.exit(1)

    try:
        asyncio.run(run_backtest(
            csv_path=args.csv_path,
            playback_speed=args.speed,
            use_mock_llm=args.mock_llm,
            use_db=args.db,
            quiet=args.quiet,
        ))
    except KeyboardInterrupt:
        print("\n用户中断。")
    except Exception:
        if logger:
            logger.critical("回测致命错误", exc_info=True)
        else:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
