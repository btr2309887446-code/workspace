"""
主程序与调度引擎 (pipeline.py)
===============================
职责：
  1. 系统唯一入口——初始化所有模块，建立 asyncio 主事件循环
  2. 5 个协程并发运行：
      a. DataFetcher    —— OKX WebSocket 实时数据推送（含无限重连）
      b. SessionPoller  —— 轮询盘口状态，动态切换订阅列表
      c. Consumer       —— 消费行情、更新计算器、检测告警、触发 LLM
      d. PeriodicReporter —— 每 5 分钟生成聚合报告
      e. StatsPrinter   —— 定期输出运行摘要
  3. 事件驱动：能量突破阈值 → asyncio.create_task(llm_agent.analyze) 非阻塞触发
  4. 盘口过滤：底层现货休市期间拦截伪动能信号，避免 LLM 被误导
  5. 捕获 SIGINT/SIGTERM，安全关闭 WebSocket 和所有协程

运行方式：
  python pipeline.py

环境变量：
  LLM_API_KEY=sk-xxx    LLM_API_ENDPOINT=https://...
  ENERGY_THRESHOLD=5000
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Dict, Optional, Set

from config import get_settings, setup_logging, Ansi
from session_manager import MarketSession, SessionState
from data_fetcher import SyntheticEquityFetcher
from analytics import MarketDynamicsCalculator
from llm_agent import MarketLLMAgent
from reporter import ReportGenerator, print_energy_alert, print_llm_decision

logger: Optional[logging.Logger] = None


# ══════════════════════════════════════════════════════════════════════════
# 盘口轮询协程
# ══════════════════════════════════════════════════════════════════════════

async def session_poller(
    sm: MarketSession,
    fetcher: SyntheticEquityFetcher,
    shutdown_event: asyncio.Event,
    interval: float,
) -> None:
    """
    盘口状态机轮询协程。

    每隔 interval 秒检查底层现货市场盘口，
    将应监控的合约列表推送给 DataFetcher 进行订阅切换。

    休市期间推送空列表 → Fetcher 取消所有订阅 → Consumer 停止处理。
    """
    logger.info(f"盘口轮询已启动 | 间隔={interval}s")
    last_active: Set[str] = set()

    while not shutdown_event.is_set():
        try:
            info = sm.current_session()
            current_active = set(info.active_swaps)

            if current_active != last_active:
                await fetcher.update_subscriptions(list(current_active))
                if current_active:
                    logger.info(
                        f"盘口切换 | {info.state_name} | "
                        f"活跃={info.active_swaps} | 抑制={info.suppressed_swaps}"
                    )
                else:
                    logger.info(f"盘口: {info.state_name}（休市，已取消所有订阅）")
                last_active = current_active

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("盘口轮询异常")

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break

    logger.info("盘口轮询已退出")


# ══════════════════════════════════════════════════════════════════════════
# 消费者协程
# ══════════════════════════════════════════════════════════════════════════

async def consumer_task(
    data_queue: asyncio.Queue,
    calculators: Dict[str, MarketDynamicsCalculator],
    llm_agent: MarketLLMAgent,
    session_manager: MarketSession,
    settings,
    alert_counters: Dict[str, int],
    shutdown_event: asyncio.Event,
) -> None:
    """
    消费者协程——核心数据处理链路。

    处理流程：
      1. data_queue.get() → 获取标准化行情数据
      2. 盘口过滤——非活跃时段的数据直接丢弃（记录 suppressed 计数）
      3. 获取/创建标的计算器 → calculator.update()
      4. 若能量 > 阈值 → 触发 LLM 分析（asyncio.create_task，非阻塞）
      5. 节流控制台输出
    """
    last_print_time = 0.0
    header_printed = False
    suppressed_count = 0
    active_symbols: Set[str] = set()

    # 强引用集合 —— 防止 asyncio.create_task 产生的 Task 被 GC 意外回收
    # 每次 LLM 任务完成后通过 done callback 自动从集合中移除，确保零内存泄漏
    background_tasks: set = set()

    logger.info("消费者协程已启动")

    while not shutdown_event.is_set():
        try:
            data = await asyncio.wait_for(data_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("消费者队列读取异常")
            continue

        if data is None:
            logger.info("消费者收到毒丸信号")
            break

        symbol = data.get("symbol", "")
        if not symbol:
            continue

        # ── 盘口过滤：检查当前是否为活跃交易时段 ──
        session_info = session_manager.current_session()
        active_symbols = set(session_info.active_swaps)

        if symbol not in active_symbols:
            suppressed_count += 1
            if suppressed_count <= 1 or suppressed_count % 500 == 0:
                logger.debug(
                    f"盘口过滤: {symbol} 不在活跃列表（当前 {session_info.state_name}），"
                    f"已抑制 {suppressed_count} 条"
                )
            continue

        # ── 获取/创建计算器 ──
        if symbol not in calculators:
            calculators[symbol] = MarketDynamicsCalculator(settings)
            logger.info(f"为 {symbol} 创建计算器（首次出现）")

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
            logger.exception(f"{symbol} 计算器更新失败")
            continue

        # ── 能量告警检测 + LLM 触发 ──
        if result["initialized"] and result["energy"] > settings.energy_threshold:
            if symbol not in alert_counters:
                alert_counters[symbol] = 0

            now = time.time()
            last_alert = getattr(calc, "_last_alert_ts", 0)
            if now - last_alert >= settings.alert_cooldown_seconds:
                alert_counters[symbol] += 1
                calc._last_alert_ts = now  # type: ignore

                # 控制台彩色告警
                print_energy_alert(
                    symbol=symbol,
                    price=result["price"],
                    velocity=result["velocity"],
                    energy=result["energy"],
                    threshold=settings.energy_threshold,
                    alert_count=alert_counters[symbol],
                    timestamp=result["timestamp"],
                )

                # 非阻塞触发 LLM —— 传入 5 分钟窗口聚合数据
                now_ts = result["timestamp"]
                five_min_stats = calc.get_window_stats(now_ts - 300)
                llm_task = asyncio.create_task(
                    _llm_analyze_and_print(
                        llm_agent=llm_agent,
                        ticker=symbol,
                        current_price=result["price"],
                        velocity=result["velocity"],
                        energy=result["energy"],
                        five_min_stats=five_min_stats,
                    ),
                    name=f"llm_{symbol}",
                )
                # 持有强引用防止 GC 中途销毁；任务完成后自动释放
                background_tasks.add(llm_task)
                llm_task.add_done_callback(background_tasks.discard)

                logger.warning(
                    f"能量告警 | {symbol} | price={result['price']:,.4f} | "
                    f"energy={result['energy']:,.4f} | velocity={result['velocity']:+.4f}"
                )

        # ── 节流打印多标的行情 ──
        now = time.time()
        if now - last_print_time >= 1.0:
            if result["initialized"]:
                parts = []
                for sym, c in sorted(calculators.items()):
                    if c.is_initialized and sym in active_symbols:
                        d = "+" if c.velocity > 0.0001 else "-" if c.velocity < -0.0001 else "~"
                        parts.append(f"{sym.split('-')[0]}:{c.price:,.4f} {d}{abs(c.velocity):.4f}")
                if parts:
                    print(f"  {' | '.join(parts)}", flush=True)
            last_print_time = now

        if not header_printed and result["initialized"]:
            print(f"\n{'─' * 78}")
            print(f"  {'Contract':<22} {'Price':>12} {'Velocity':>14} {'Energy':>14}")
            print(f"{'─' * 78}")
            header_printed = True

    logger.info(f"消费者协程已退出（共抑制 {suppressed_count} 条非活跃时段消息）")


async def _llm_analyze_and_print(
    llm_agent: MarketLLMAgent,
    ticker: str,
    current_price: float,
    velocity: float,
    energy: float,
    five_min_stats: dict,
) -> None:
    """独立协程：异步调用 LLM 并打印结果。不阻塞主数据流。"""
    try:
        result = await llm_agent.analyze(
            ticker=ticker,
            current_price=current_price,
            velocity=velocity,
            energy=energy,
            five_min_stats=five_min_stats,
        )
        if result:
            print_llm_decision(result)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(f"LLM 分析协程异常 | ticker={ticker}")


# ══════════════════════════════════════════════════════════════════════════
# 统计打印协程
# ══════════════════════════════════════════════════════════════════════════

async def stats_printer(
    session_manager: MarketSession,
    calculators: dict,
    fetcher: SyntheticEquityFetcher,
    llm_agent: MarketLLMAgent,
    alert_counters: dict,
    shutdown_event: asyncio.Event,
    interval: float,
) -> None:
    """每隔 interval 秒输出运行摘要。"""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        if shutdown_event.is_set():
            break
        try:
            _print_stats(session_manager, calculators, fetcher, llm_agent, alert_counters)
        except Exception:
            logger.exception("统计打印异常")


def _print_stats(sm, calculators, fetcher, llm_agent, alert_counters) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    info = sm.current_session()
    s = fetcher.stats
    llm_s = llm_agent.get_stats() if llm_agent else {}

    print(f"\n{Ansi.BLUE}{'─' * 64}")
    print(f"  STATUS REPORT | {now} | {info.state_name}")
    print(f"{'─' * 64}{Ansi.RESET}")

    if info.active_swaps:
        print(f"  Active      : {info.active_swaps}")
    if info.suppressed_swaps:
        print(f"  Suppressed  : {info.suppressed_swaps}")

    for sym, calc in sorted(calculators.items()):
        if calc.is_initialized:
            a = alert_counters.get(sym, 0)
            print(
                f"  {sym:<24} price={calc.price:>10,.4f} "
                f"vel={calc.velocity:>+9.4f} "
                f"energy={calc.energy:>10.4f} "
                f"alerts={a} "
                f"samples={calc.sample_count}"
            )

    print(f"  ── Connection ──")
    print(f"    Msg: rcvd={s.get('messages_received', 0):,}  "
          f"parsed={s.get('messages_parsed', 0):,}  "
          f"dropped={s.get('messages_dropped', 0)}  "
          f"reconn={s.get('reconnect_attempts', 0)}")

    if llm_s:
        print(f"  ── LLM Agent ──")
        print(f"    Calls={llm_s.get('total_calls', 0)}  "
              f"Success={llm_s.get('success', 0)}  "
              f"Fail={llm_s.get('failure', 0)}  "
              f"Timeout={llm_s.get('timeout', 0)}  "
              f"Cooldown={llm_s.get('skipped_cooldown', 0)}")
        active_cd = llm_s.get("cooldown_active", {})
        if active_cd:
            cd_parts = [f"{k}:{v}s" for k, v in active_cd.items()]
            print(f"    Cooldowns: {', '.join(cd_parts)}")

    print()


# ══════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global logger

    # 1. 配置与日志
    settings = get_settings()
    setup_logging(settings)
    logger = logging.getLogger("SwapMomentum.Pipeline")
    _print_banner(settings)

    # 2. 实例化组件
    session_manager = MarketSession(settings)
    data_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    fetcher = SyntheticEquityFetcher(settings, data_queue)
    llm_agent = MarketLLMAgent(
        api_endpoint=settings.llm_api_endpoint,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )
    report_generator = ReportGenerator(settings, session_manager, llm_agent)

    calculators: Dict[str, MarketDynamicsCalculator] = {}
    alert_counters: Dict[str, int] = {}

    # 3. 停机信号
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler(sig, frame):
        logger.info(f"收到信号 signal={sig}，准备优雅停机...")
        loop.call_soon_threadsafe(shutdown_event.set)

    try:
        loop.add_signal_handler(signal.SIGINT, lambda: shutdown_event.set())
        loop.add_signal_handler(signal.SIGTERM, lambda: shutdown_event.set())
    except NotImplementedError:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    # 4. 启动并发协程
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(fetcher.start(), name="DataFetcher"))
    tasks.append(asyncio.create_task(
        session_poller(session_manager, fetcher, shutdown_event, settings.session_check_interval),
        name="SessionPoller",
    ))
    tasks.append(asyncio.create_task(
        consumer_task(data_queue, calculators, llm_agent, session_manager,
                      settings, alert_counters, shutdown_event),
        name="Consumer",
    ))
    tasks.append(asyncio.create_task(
        report_generator.run(shutdown_event, calculators, fetcher),
        name="PeriodicReporter",
    ))
    tasks.append(asyncio.create_task(
        stats_printer(session_manager, calculators, fetcher, llm_agent,
                      alert_counters, shutdown_event, settings.stats_interval),
        name="StatsPrinter",
    ))

    logger.info(f"所有任务已启动（共 {len(tasks)} 个），系统运行中...")
    print(f"\n{Ansi.CYAN}等待盘口调度与行情数据...{Ansi.RESET}\n")

    # 5. 等待停机
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("收到停机信号，开始有序关闭...")

    # 6. 有序关闭
    await fetcher.stop()

    for task in tasks:
        if not task.done():
            task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.error(f"任务 {task.get_name()} 异常退出: {result}")

    logger.info("所有任务已安全退出")
    print(f"\n{Ansi.GREEN}程序已安全关闭。{Ansi.RESET}")


# ══════════════════════════════════════════════════════════════════════════
# 辅助输出
# ══════════════════════════════════════════════════════════════════════════

def _print_banner(settings) -> None:
    llm_status = f"Configured ({settings.llm_model})" if settings.llm_configured else "Not configured"
    banner = f"""
{Ansi.CYAN}+==============================================================================+
|      SYNTHETIC EQUITY SWAP MOMENTUM TRACKER v1.0                         |
|        股权代币永续合约动量实时监控系统                                      |
+------------------------------------------------------------------------------+
|  KRX Swaps  : {', '.join(settings.krx_swaps):<60}|
|  US Swaps   : {', '.join(settings.us_swaps):<60}|
|  OKX WS     : {settings.okx_ws_public_url:<60}|
|  LLM Engine : {llm_status:<60}|
|  Energy Thr : {settings.energy_threshold:<60,.0f}|
|  Report Int : {settings.report_interval_seconds:<60}s|
|  Log Level  : {settings.log_level:<60}|
+==============================================================================+
  Press Ctrl+C to Exit | Reports saved to '{settings.report_dir}/'
{Ansi.RESET}"""
    print(banner, flush=True)
    logger.info("Synthetic Equity Swap Momentum Tracker v1.0 starting")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断，程序退出。")
    except Exception:
        if logger is not None:
            logger.critical("程序发生致命错误", exc_info=True)
        else:
            import traceback
            traceback.print_exc()
        sys.exit(1)
