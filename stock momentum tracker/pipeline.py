"""
主程序与调度引擎 (pipeline.py)
===============================
职责：
  1. 系统唯一入口——初始化所有模块，建立 asyncio 主事件循环
  2. 并发任务管理——以下协程独立并发运行：
      a. Session Poller   —— 定期检查盘口状态，动态切换监控标的
      b. Data Fetcher     —— 根据活跃标的池获取实时行情
      c. Consumer         —— 消费行情数据、更新计算器、触发 LLM
      d. Periodic Reporter—— 每 5 分钟生成聚合报告
      e. Stats Printer    —— 定期输出运行摘要
  3. 事件驱动架构——能量突破阈值时非阻塞地触发 LLM 分析
  4. 捕获系统中断信号，实现优雅停机

运行方式：
  python pipeline.py

前置条件：
  1. 安装依赖：pip install aiohttp yfinance pytz aiofiles
  2. (可选) 配置 Alpaca API Key → 环境变量 ALPACA_API_KEY / ALPACA_API_SECRET
  3. (可选) 配置 LLM API Key   → 环境变量 LLM_API_KEY
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Dict, Optional

from config import get_settings, setup_logging, Ansi
from session_manager import MarketSession, SessionState
from data_fetcher import EquityDataFetcher
from analytics import MarketDynamicsCalculator
from llm_agent import MarketLLMAgent
from reporter import (
    ReportGenerator,
    print_energy_alert,
    print_llm_decision,
)

logger: Optional[logging.Logger] = None


# ══════════════════════════════════════════════════════════════════════════
# 盘口轮询协程
# ══════════════════════════════════════════════════════════════════════════

async def session_poller(
    session_manager: MarketSession,
    fetcher: EquityDataFetcher,
    shutdown_event: asyncio.Event,
    interval: float,
) -> None:
    """
    盘口状态机轮询协程。

    每隔 interval 秒检查当前盘口，若标的池发生变化，
    通知 DataFetcher 切换轮询列表。

    休市期间停止所有 API 请求，节省配额。
    """
    logger.info(f"盘口轮询已启动 | 间隔={interval}s")

    while not shutdown_event.is_set():
        try:
            info = session_manager.current_session()

            # 通知数据获取器切换标的
            await fetcher.update_symbols(info.active_symbols, info.state)

            # 输出盘口信息
            if info.active_symbols:
                logger.info(
                    f"盘口: {info.state_name} | "
                    f"活跃标的={info.active_symbols} | "
                    f"下次切换={info.next_transition_utc}"
                )
            else:
                logger.info(f"盘口: {info.state_name}（休市中）")

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
# 消费者协程：数据处理 + 告警 + LLM 触发
# ══════════════════════════════════════════════════════════════════════════

async def consumer_task(
    data_queue: asyncio.Queue,
    calculators: Dict[str, MarketDynamicsCalculator],
    llm_agent: MarketLLMAgent,
    settings,
    alert_counters: Dict[str, int],
    shutdown_event: asyncio.Event,
) -> None:
    """
    消费者协程——从数据队列取出行情，更新计算器，检测并处理告警。

    处理流程：
      1. data_queue.get() → 获取标准化行情数据
      2. 获取/创建对应标的的 MarketDynamicsCalculator
      3. calculator.update() → 更新速度 & 能量
      4. 若能量 > 阈值 → 触发 LLM 分析（非阻塞 asyncio.create_task）
      5. 节流输出到控制台
    """
    last_print_time: float = 0.0
    header_printed: bool = False

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

        # 获取/创建计算器
        if symbol not in calculators:
            calculators[symbol] = MarketDynamicsCalculator(settings)
            logger.info(f"为 {symbol} 创建新的计算器")

        calc = calculators[symbol]

        # 更新计算器
        try:
            timestamp = (
                data.get("server_timestamp")
                or data.get("local_timestamp", time.time())
            )
            result = calc.update(
                price=data["price"],
                volume=data["volume"],
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
            last = getattr(calc, "_last_alert_ts", 0)
            if now - last >= settings.alert_cooldown_seconds:
                alert_counters[symbol] += 1
                calc._last_alert_ts = now  # type: ignore

                # 控制台告警
                print_energy_alert(
                    symbol=symbol,
                    price=result["price"],
                    velocity=result["velocity"],
                    energy=result["energy"],
                    threshold=settings.energy_threshold,
                    alert_count=alert_counters[symbol],
                    timestamp=result["timestamp"],
                )

                # 异步触发 LLM 分析（非阻塞）
                recent = calc.get_recent_snapshot(n=10)
                asyncio.create_task(
                    _llm_analyze_and_print(
                        llm_agent=llm_agent,
                        symbol=symbol,
                        price=result["price"],
                        velocity=result["velocity"],
                        energy=result["energy"],
                        recent=recent,
                    ),
                    name=f"llm_{symbol}",
                )

                logger.warning(
                    f"能量告警 | {symbol} | price={result['price']:,.2f} | "
                    f"energy={result['energy']:,.2f} | velocity={result['velocity']:+.4f}"
                )

        # ── 节流输出 ──
        now = time.time()
        if now - last_print_time >= 1.0:
            if result["initialized"]:
                # 构建多标的单行输出
                symbols_line = []
                for sym, c in calculators.items():
                    if c.is_initialized:
                        direction = "+" if c.velocity > 0.0001 else "-" if c.velocity < -0.0001 else "~"
                        symbols_line.append(
                            f"{sym}:{c.price:,.2f} {direction}{abs(c.velocity):.4f}"
                        )
                if symbols_line:
                    print(f"  {' | '.join(symbols_line)}", flush=True)
            last_print_time = now

        if not header_printed and result["initialized"]:
            print(f"\n{'─' * 72}")
            print(f"  {'Symbol':<8} {'Price':>12} {'Velocity':>12} {'Energy':>14} {'Source'}")
            print(f"{'─' * 72}")
            header_printed = True

    logger.info("消费者协程已退出")


async def _llm_analyze_and_print(
    llm_agent: MarketLLMAgent,
    symbol: str,
    price: float,
    velocity: float,
    energy: float,
    recent: dict,
) -> None:
    """
    异步调用 LLM 分析并打印结果。

    作为独立协程运行，不阻塞主消费者循环。
    即使 LLM 调用失败或超时，也不影响数据接收与计算。
    """
    try:
        result = await llm_agent.analyze(
            symbol=symbol,
            price=price,
            velocity=velocity,
            energy=energy,
            recent_snapshot=recent,
        )
        if result:
            print_llm_decision(result)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(f"LLM 分析协程异常 | symbol={symbol}")


# ══════════════════════════════════════════════════════════════════════════
# 统计打印协程
# ══════════════════════════════════════════════════════════════════════════

async def stats_printer(
    session_manager: MarketSession,
    calculators: dict,
    fetcher: EquityDataFetcher,
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


def _print_stats(session_manager, calculators, fetcher, llm_agent, alert_counters) -> None:
    """输出当前运行摘要。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    info = session_manager.current_session()
    s = fetcher.stats
    llm_s = llm_agent.get_stats() if llm_agent else {}

    print(f"\n{Ansi.BLUE}{'─' * 64}")
    print(f"  STATUS REPORT | {now} | {info.state_name}")
    print(f"{'─' * 64}{Ansi.RESET}")

    for sym, calc in calculators.items():
        if calc.is_initialized:
            vs = calc.get_velocity_stats()
            es = calc.get_energy_stats()
            alerts = alert_counters.get(sym, 0)
            print(
                f"  {sym:<8} price={calc.price:>10,.2f} "
                f"vel={calc.velocity:>+9.4f} "
                f"energy={calc.energy:>10.2f} "
                f"alerts={alerts} "
                f"samples={calc.sample_count}"
            )

    print(f"  ── Connection ──")
    print(f"    Msg rcvd={s.get('messages_received', 0):,}  "
          f"Alpaca={s.get('alpaca_requests', 0):,}  "
          f"YF={s.get('yf_requests', 0):,}  "
          f"Errors={s.get('connection_errors', 0)}")

    if llm_s:
        print(f"  ── LLM Agent ──")
        print(f"    Calls={llm_s.get('total_calls', 0)}  "
              f"Success={llm_s.get('success', 0)}  "
              f"Fail={llm_s.get('failure', 0)}  "
              f"Cooldown={llm_s.get('skipped_cooldown', 0)}")
        active_cd = llm_s.get("cooldown_active", {})
        if active_cd:
            cd_str = ", ".join(f"{k}:{v}s" for k, v in active_cd.items())
            print(f"    Cooldowns: {cd_str}")

    print()


# ══════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    """程序主入口。"""
    global logger

    # ── 1. 配置与日志 ──
    settings = get_settings()
    setup_logging(settings)
    logger = logging.getLogger("StockMomentum.Pipeline")

    _print_banner(settings)

    # ── 2. 实例化组件 ──
    session_manager = MarketSession(settings)
    data_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    fetcher = EquityDataFetcher(settings, data_queue)
    llm_agent = MarketLLMAgent(settings)
    report_generator = ReportGenerator(settings, session_manager, llm_agent)

    # 每标的计算器 + 告警计数
    calculators: Dict[str, MarketDynamicsCalculator] = {}
    alert_counters: Dict[str, int] = {}

    # ── 3. 停机信号 ──
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler(sig, frame):
        logger.info(f"收到系统信号 signal={sig}，准备优雅停机...")
        loop.call_soon_threadsafe(shutdown_event.set)

    try:
        loop.add_signal_handler(signal.SIGINT, lambda: shutdown_event.set())
        loop.add_signal_handler(signal.SIGTERM, lambda: shutdown_event.set())
    except NotImplementedError:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    # ── 4. 启动并发协程 ──
    tasks: list[asyncio.Task] = []

    tasks.append(asyncio.create_task(
        session_poller(session_manager, fetcher, shutdown_event, settings.session_check_interval),
        name="SessionPoller",
    ))
    tasks.append(asyncio.create_task(
        consumer_task(data_queue, calculators, llm_agent, settings, alert_counters, shutdown_event),
        name="Consumer",
    ))
    tasks.append(asyncio.create_task(
        report_generator.run(shutdown_event, calculators, fetcher),
        name="PeriodicReporter",
    ))
    tasks.append(asyncio.create_task(
        stats_printer(session_manager, calculators, fetcher, llm_agent, alert_counters,
                      shutdown_event, settings.stats_interval),
        name="StatsPrinter",
    ))

    logger.info(f"所有任务已启动（共 {len(tasks)} 个），系统运行中...")
    print(f"\n{Ansi.CYAN}等待第一个盘口周期...{Ansi.RESET}\n")

    # ── 5. 等待停机 ──
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("主循环被取消")

    logger.info("收到停机信号，开始有序关闭...")

    # ── 6. 有序关闭 ──
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
    """打印启动横幅。"""
    alpaca_status = "已配置" if settings.alpaca_configured else "未配置（使用 yfinance）"
    llm_status = f"已配置 ({settings.llm_model})" if settings.llm_configured else "未配置"

    banner = f"""
{Ansi.CYAN}+==============================================================================+
|          GLOBAL SEMICONDUCTOR & TECH MOMENTUM TRACKER v1.0               |
|              全球半导体及科技巨头动量实时监控系统                            |
+------------------------------------------------------------------------------+
|  KRX Symbols : {', '.join(settings.krx_symbols):<60}|
|  US Symbols  : {', '.join(settings.us_symbols):<60}|
|  Alpaca API  : {alpaca_status:<60}|
|  LLM Engine  : {llm_status:<60}|
|  Energy Thr  : {settings.energy_threshold:<60,.0f}|
|  Report Int  : {settings.report_interval_seconds:<60}s|
|  Log Level   : {settings.log_level:<60}|
+==============================================================================+
  Press Ctrl+C to Exit | Reports saved to '{settings.report_dir}/'
{Ansi.RESET}"""
    print(banner, flush=True)
    logger.info("Global Semiconductor & Tech Momentum Tracker v1.0 starting")


# ══════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════

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
