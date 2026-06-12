"""
报告与持久化模块 (reporter.py)
=================================
职责：
  1. 实时日志——记录能量异动，格式化打印 LLM 决策结果
  2. 5 分钟周期报告——聚合多标的动能分析，生成结构化 .txt 报告
  3. 异步文件 I/O——使用 asyncio.to_thread() 分流到线程池，绝不阻塞主循环

设计原则：
  - 所有 I/O 操作完全异步，与数据接收、计算、LLM 调用并发运行
  - 报告内容包含每只活跃标的的平均速度、能量积分、极值点、趋势方向
  - 文件命名按时间戳，自动创建 reports/ 目录
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from config import Settings, Ansi
from session_manager import SessionState

logger = logging.getLogger("StockMomentum.Reporter")


# ══════════════════════════════════════════════════════════════════════════
# 5 分钟周期报告生成器
# ══════════════════════════════════════════════════════════════════════════

class ReportGenerator:
    """
    5 分钟周期聚合报告生成器。

    每隔 report_interval_seconds 自动触发一次，
    遍历所有活跃标的的 MarketDynamicsCalculator，
    获取窗口聚合数据，生成结构化 .txt 报告并异步写入磁盘。
    """

    def __init__(
        self,
        settings: Settings,
        session_manager,
        llm_agent,
    ):
        """
        初始化报告生成器。

        Args:
            settings:       系统配置
            session_manager: MarketSession 实例
            llm_agent:       MarketLLMAgent 实例（读取调用统计）
        """
        self.settings = settings
        self.session_manager = session_manager
        self.llm_agent = llm_agent

        self.interval: int = settings.report_interval_seconds
        self.report_dir: Path = Path(settings.report_dir)

        self._last_report_ts: float = 0.0
        self._report_index: int = 0

        self.report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 主运行循环
    # ------------------------------------------------------------------

    async def run(
        self,
        shutdown_event: asyncio.Event,
        calculators: dict,
        fetcher,
    ) -> None:
        """
        周期报告主循环。

        Args:
            shutdown_event: 停机信号
            calculators:    {symbol: MarketDynamicsCalculator} 字典
            fetcher:        EquityDataFetcher 实例
        """
        self._last_report_ts = time.time()

        logger.info(
            f"周期报告生成器已启动 | 间隔={self.interval}s | "
            f"目录={self.report_dir}"
        )

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
                logger.exception("报告生成失败，跳过本次")

        logger.info("周期报告生成器已退出")

    # ------------------------------------------------------------------
    # 内部：报告生成
    # ------------------------------------------------------------------

    async def _generate_and_save(
        self, calculators: dict, fetcher
    ) -> None:
        """生成并保存一份多标的聚合报告。"""
        now = time.time()
        session_info = self.session_manager.current_session()

        # 收集每只标的的窗口数据
        symbol_reports = {}
        for symbol, calc in calculators.items():
            if not calc.is_initialized:
                continue
            stats = calc.get_window_stats(self._last_report_ts)
            if stats is not None and stats["sample_count"] >= 2:
                symbol_reports[symbol] = stats

        self._last_report_ts = now
        self._report_index += 1

        if not symbol_reports:
            logger.debug("无有效数据，跳过本次报告")
            return

        # 格式化报告
        content = self._format_aggregate_report(
            symbol_reports, session_info, fetcher
        )

        # 异步写入
        filename = self._make_filename()
        await self._save_report(content, filename)

        # 控制台摘要
        symbols_str = ", ".join(symbol_reports.keys())
        logger.info(
            f"报告 #{self._report_index} 已生成 | "
            f"标的={symbols_str} | "
            f"文件={filename}"
        )

    def _format_aggregate_report(
        self,
        symbol_reports: Dict[str, Dict[str, Any]],
        session_info,
        fetcher,
    ) -> str:
        """
        格式化为多标的聚合报告。

        Args:
            symbol_reports: {symbol: window_stats_dict}
            session_info:   MarketSession.current_session()
            fetcher:        数据获取器

        Returns:
            格式化的文本报告
        """
        generated_dt = datetime.now()
        llm_stats = self.llm_agent.get_stats() if self.llm_agent else {}

        lines = []
        lines.append("=" * 64)
        lines.append("  全球半导体及科技巨头动量监控报告")
        lines.append("=" * 64)
        lines.append("")
        lines.append(f"  报告生成时间: {generated_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"  当前盘口    : {session_info.state_name}")
        lines.append(f"  活跃标的数  : {len(symbol_reports)}")
        lines.append("")

        for symbol, stats in symbol_reports.items():
            lines.append(f"{'─' * 64}")
            lines.append(f"  [{symbol}]")
            lines.append(f"{'─' * 64}")
            start_dt = datetime.fromtimestamp(stats["start_time"])
            end_dt = datetime.fromtimestamp(stats["end_time"])
            duration = stats["end_time"] - stats["start_time"]

            price_prefix = "+" if stats["price_change_pct"] > 0 else ""
            lines.append(f"  周期      : {start_dt.strftime('%H:%M:%S')} ~ "
                         f"{end_dt.strftime('%H:%M:%S')}  ({duration:.0f}s)")
            lines.append(f"  采样数    : {stats['sample_count']:,}")
            lines.append(f"  起始价    : {stats['first_price']:>12,.2f} USD")
            lines.append(f"  最新价    : {stats['last_price']:>12,.2f} USD")
            lines.append(f"  涨跌幅    : {price_prefix}{stats['price_change_pct']:>10.4f} %")
            lines.append(f"  总成交量  : {stats['total_volume']:>12,.0f} 股")
            lines.append(f"  平均速度  : {stats['avg_velocity']:>12.6f} USD/s")
            lines.append(f"  速度标准差: {stats['std_velocity']:>12.6f}")
            lines.append(f"  能量积分  : {stats['energy_integral']:>12,.2f}  (∫E dt)")
            lines.append(f"  平均能量  : {stats['avg_energy']:>12.2f}")
            lines.append(f"  最高能量  : {stats['max_energy']:>12,.2f}  @ "
                         f"{datetime.fromtimestamp(stats['max_energy_ts']).strftime('%H:%M:%S')} "
                         f"(价格 {stats['max_energy_price']:,.2f})")
            lines.append(f"  趋势方向  : {stats['direction']}")
            lines.append(f"  买盘占比  : {stats['bull_ratio']:.1f}%")
            lines.append("")

        # 运行状态
        lines.append(f"{'─' * 64}")
        lines.append(f"  [运行状态]")
        lines.append(f"{'─' * 64}")
        lines.append(f"  接收消息  : {fetcher.stats.get('messages_received', 0):,}")
        lines.append(f"  Alpaca API: {fetcher.stats.get('alpaca_requests', 0):,}")
        lines.append(f"  YF 轮询   : {fetcher.stats.get('yf_requests', 0):,}")
        lines.append(f"  连接错误  : {fetcher.stats.get('connection_errors', 0)}")

        if llm_stats:
            lines.append(f"  LLM 调用  : total={llm_stats['total_calls']} "
                         f"success={llm_stats['success']} "
                         f"fail={llm_stats['failure']} "
                         f"cooldown_skip={llm_stats['skipped_cooldown']}")

        lines.append("")
        lines.append("=" * 64)
        lines.append("  报告结束")
        lines.append("=" * 64)
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部：文件 I/O
    # ------------------------------------------------------------------

    def _make_filename(self) -> str:
        """根据模板生成报告文件名。"""
        ts_str = datetime.now().strftime("%Y%m%d_%H%M")
        filename = self.settings.report_filename_template
        filename = filename.replace("{timestamp}", ts_str)
        return filename

    async def _save_report(self, content: str, filename: str) -> None:
        """异步将报告写入磁盘（线程池分流）。"""
        filepath = self.report_dir / filename
        await asyncio.to_thread(self._write_file_sync, content, filepath)

    @staticmethod
    def _write_file_sync(content: str, filepath: Path) -> None:
        """同步文件写入（在线程池中执行）。"""
        filepath.write_text(content, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 实时告警打印辅助函数
# ══════════════════════════════════════════════════════════════════════════

def print_energy_alert(
    symbol: str,
    price: float,
    velocity: float,
    energy: float,
    threshold: float,
    alert_count: int,
    timestamp: float,
) -> None:
    """
    在控制台以高亮颜色输出能量异动告警。

    Args:
        symbol:      股票代码
        price:       当前价格
        velocity:    当前速度
        energy:      当前能量
        threshold:   触发阈值
        alert_count: 累计告警次数
        timestamp:   时间戳
    """
    direction = "BUY" if velocity > 0 else "SELL"
    if direction == "BUY":
        dir_color = Ansi.BOLD_GREEN
    else:
        dir_color = Ansi.BOLD_RED

    ts_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"{Ansi.BOLD_YELLOW}{'!' * 60}{Ansi.RESET}",
        f"{Ansi.BOLD_YELLOW}!!! ENERGY ALERT #{alert_count} | {symbol} | {direction} !!!{Ansi.RESET}",
        f"  Time      : {ts_str}",
        f"  Price     : {price:,.2f} USD",
        f"  Energy    : {energy:,.2f}  (threshold: {threshold:,.0f})",
        f"  Velocity  : {velocity:+.6f} USD/s",
        f"  Direction : {dir_color}{direction}{Ansi.RESET}",
        f"  Source    : Triggering LLM analysis...",
        f"{Ansi.BOLD_YELLOW}{'!' * 60}{Ansi.RESET}",
    ]
    print("\n".join(lines), flush=True)


def print_llm_decision(result: Dict[str, Any]) -> None:
    """
    在控制台以格式化方式输出 LLM 决策结果。

    Args:
        result: MarketLLMAgent.analyze() 的返回值
    """
    action = result.get("action", "HOLD")
    confidence = result.get("confidence", 0)
    reasoning = result.get("reasoning", "")
    symbol = result.get("symbol", "???")
    price = result.get("price", 0)

    if action == "BUY":
        color = Ansi.BOLD_GREEN
    elif action == "SELL":
        color = Ansi.BOLD_RED
    else:
        color = Ansi.YELLOW

    print(
        f"{Ansi.BOLD_CYAN}[LLM] {symbol} | {color}{action}{Ansi.BOLD_CYAN} "
        f"| confidence={confidence:.2f} | "
        f"price={price:,.2f} | "
        f"{reasoning}{Ansi.RESET}",
        flush=True,
    )
