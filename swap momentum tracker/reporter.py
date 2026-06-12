"""
报告与持久化模块 (reporter.py)
=================================
职责：
  1. 实时日志——格式化输出能量异动 + LLM 决策结果
  2. 5 分钟周期报告——遍历所有活跃标的的聚合数据，生成 .txt 报告
  3. 异步文件 I/O——使用 asyncio.to_thread() 分流到线程池
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from config import Settings, Ansi

logger = logging.getLogger("SwapMomentum.Reporter")


class ReportGenerator:
    """
    5 分钟周期聚合报告生成器。
    """

    def __init__(self, settings: Settings, session_manager, llm_agent):
        self.settings = settings
        self.session_manager = session_manager
        self.llm_agent = llm_agent
        self.interval: int = settings.report_interval_seconds
        self.report_dir: Path = Path(settings.report_dir)
        self._last_report_ts: float = 0.0
        self._report_index: int = 0
        self.report_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self, shutdown_event: asyncio.Event, calculators: dict, fetcher
    ) -> None:
        """周期报告主循环。"""
        self._last_report_ts = time.time()
        logger.info(f"报告生成器启动 | 间隔={self.interval}s | 目录={self.report_dir}")

        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            if shutdown_event.is_set():
                break
            try:
                await self._generate_and_save(calculators, fetcher)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("报告生成失败，跳过")

        logger.info("报告生成器已退出")

    async def _generate_and_save(self, calculators: dict, fetcher) -> None:
        now = time.time()
        info = self.session_manager.current_session()

        symbol_reports = {}
        for sym, calc in calculators.items():
            if not calc.is_initialized:
                continue
            stats = calc.get_window_stats(self._last_report_ts)
            if stats and stats["sample_count"] >= 2:
                symbol_reports[sym] = stats

        self._last_report_ts = now
        self._report_index += 1

        if not symbol_reports:
            return

        content = self._format_report(symbol_reports, info, fetcher)
        filename = self._make_filename()
        await self._save_report(content, filename)

        logger.info(f"报告 #{self._report_index} | 标的={list(symbol_reports.keys())} | 文件={filename}")

    def _format_report(self, symbol_reports: dict, info, fetcher) -> str:
        generated_dt = datetime.now()
        llm_s = self.llm_agent.get_stats() if self.llm_agent else {}

        lines = [
            "=" * 64,
            "  股权代币永续合约动量监控报告",
            "=" * 64,
            "",
            f"  生成时间    : {generated_dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"  当前盘口    : {info.state_name}",
            f"  活跃合约数  : {len(symbol_reports)}",
            f"  抑制合约数  : {len(info.suppressed_swaps)}",
            f"  抑制清单    : {', '.join(info.suppressed_swaps) if info.suppressed_swaps else '无'}",
            "",
        ]

        for sym, s in symbol_reports.items():
            start_dt = datetime.fromtimestamp(s["start_time"])
            end_dt = datetime.fromtimestamp(s["end_time"])
            dur = s["end_time"] - s["start_time"]
            pfx = "+" if s["price_change_pct"] > 0 else ""

            lines.extend([
                f"{'─' * 64}",
                f"  [{sym}]",
                f"{'─' * 64}",
                f"  周期      : {start_dt.strftime('%H:%M:%S')} ~ {end_dt.strftime('%H:%M:%S')} ({dur:.0f}s)",
                f"  采样数    : {s['sample_count']:,}",
                f"  起始价    : {s['first_price']:>12,.4f} USDT",
                f"  最新价    : {s['last_price']:>12,.4f} USDT",
                f"  涨跌幅    : {pfx}{s['price_change_pct']:>10.4f} %",
                f"  总成交额  : {s['total_volume']:>12,.2f} USDT",
                f"  平均速度  : {s['avg_velocity']:>12.6f} USDT/s",
                f"  速度标准差: {s['std_velocity']:>12.6f}",
                f"  能量积分  : {s['energy_integral']:>12,.2f}  (∫E dt)",
                f"  平均能量  : {s['avg_energy']:>12.4f}",
                f"  最高能量  : {s['max_energy']:>12,.4f}  @ "
                f"{datetime.fromtimestamp(s['max_energy_ts']).strftime('%H:%M:%S')} "
                f"(价格 {s['max_energy_price']:,.4f})",
                f"  趋势方向  : {s['direction']}",
                f"  买盘占比  : {s['bull_ratio']:.1f}%",
                "",
            ])

        lines.extend([
            f"{'─' * 64}",
            f"  [运行状态]",
            f"{'─' * 64}",
            f"  接收消息  : {fetcher.stats.get('messages_received', 0):,}",
            f"  解析成功  : {fetcher.stats.get('messages_parsed', 0):,}",
            f"  丢弃消息  : {fetcher.stats.get('messages_dropped', 0)}",
            f"  重连次数  : {fetcher.stats.get('reconnect_attempts', 0)}",
        ])

        if llm_s:
            lines.append(
                f"  LLM 调用  : total={llm_s['total_calls']} "
                f"success={llm_s['success']} fail={llm_s['failure']} "
                f"cooldown_skip={llm_s['skipped_cooldown']}"
            )

        lines.extend(["", "=" * 64, "  报告结束", "=" * 64, ""])
        return "\n".join(lines)

    def _make_filename(self) -> str:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M")
        return self.settings.report_filename_template.replace("{timestamp}", ts_str)

    async def _save_report(self, content: str, filename: str) -> None:
        filepath = self.report_dir / filename
        await asyncio.to_thread(self._write_file_sync, content, filepath)

    @staticmethod
    def _write_file_sync(content: str, filepath: Path) -> None:
        filepath.write_text(content, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 实时告警打印
# ══════════════════════════════════════════════════════════════════════════

def print_energy_alert(
    symbol: str, price: float, velocity: float, energy: float,
    threshold: float, alert_count: int, timestamp: float,
) -> None:
    direction = "拉升" if velocity > 0 else "砸盘"
    dir_color = Ansi.BOLD_GREEN if velocity > 0 else Ansi.BOLD_RED
    ts_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"{Ansi.BOLD_YELLOW}{'!' * 60}{Ansi.RESET}",
        f"{Ansi.BOLD_YELLOW}!!! ENERGY ALERT #{alert_count} | {symbol} | {direction} !!!{Ansi.RESET}",
        f"  Time      : {ts_str}",
        f"  Price     : {price:,.4f} USDT",
        f"  Energy    : {energy:,.4f}  (threshold: {threshold:,.0f})",
        f"  Velocity  : {velocity:+.6f} USDT/s",
        f"  Direction : {dir_color}{direction}{Ansi.RESET}",
        f"  Source    : Triggering LLM analysis...",
        f"{Ansi.BOLD_YELLOW}{'!' * 60}{Ansi.RESET}",
    ]
    print("\n".join(lines), flush=True)


def print_llm_decision(result: Dict[str, Any]) -> None:
    action = result.get("action", "HOLD")
    confidence = result.get("confidence", 0)
    reasoning = result.get("reasoning", "")
    ticker = result.get("ticker", "???")
    price = result.get("price", 0)

    color_map = {"BUY": Ansi.BOLD_GREEN, "SELL": Ansi.BOLD_RED, "HOLD": Ansi.YELLOW}
    color = color_map.get(action, Ansi.YELLOW)

    print(
        f"{Ansi.BOLD_CYAN}[LLM] {ticker} | {color}{action}{Ansi.BOLD_CYAN} "
        f"| conf={confidence:.2f} | price={price:,.4f} | {reasoning}{Ansi.RESET}",
        flush=True,
    )
