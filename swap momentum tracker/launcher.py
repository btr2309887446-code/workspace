"""
统一启动器 (launcher.py)
=========================
职责：
  1. 单一入口统一管理三种运行模式
  2. 模式 A (panel) → 启动 FastAPI 后端 + Streamlit 前端面板
  3. 模式 B (live)  → 启动命令行实时监控引擎
  4. 模式 C (backtest) → 启动历史回测沙盒

用法：
  python launcher.py panel              # 启动可视化面板
  python launcher.py live               # 启动命令行实时监控
  python launcher.py backtest <CSV> --speed 10 --mock-llm   # 回测

等同于：
  python pipeline.py                    # 模式 B
  python run_backtest.py <CSV> ...      # 模式 C
"""

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── 确保项目根在 path 中 ──
sys.path.insert(0, str(Path(__file__).parent))

from config import get_settings, setup_logging, Ansi

logger = logging.getLogger("SwapMomentum.Launcher")


# ============================================================================
# 模式 A：可视化面板
# ============================================================================

def _launch_panel(host: str = "0.0.0.0", port: int = 8000):
    """
    启动 FastAPI 后端 + Streamlit 前端。

    后端通过 uvicorn 启动（子进程），前端通过 streamlit run 启动（子进程）。
    Ctrl+C 时会同时终止两个子进程。
    """
    print(f"\n{Ansi.CYAN}{'=' * 60}")
    print(f"  启动可视化面板")
    print(f"{'=' * 60}")
    print(f"  后端 API : http://{host}:{port}")
    print(f"  前端面板 : http://localhost:8501")
    print(f"  按 Ctrl+C 停止所有服务")
    print(f"{'=' * 60}{Ansi.RESET}\n")

    # 启动后端
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api_server:app",
         "--host", host, "--port", str(port)],
        cwd=str(Path(__file__).parent),
    )
    print(f"  [API] 后端已启动 (PID={api_proc.pid})")

    # 等待后端就绪
    time.sleep(2)

    # 启动前端
    dashboard_proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "dashboard.py",
         "--server.headless", "true"],
        cwd=str(Path(__file__).parent),
    )
    print(f"  [UI]  面板已启动 (PID={dashboard_proc.pid})")

    # 等待子进程
    try:
        api_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{Ansi.YELLOW}正在停止服务...{Ansi.RESET}")
        for proc, name in [(api_proc, "API"), (dashboard_proc, "UI")]:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                print(f"  [{name}] 已停止")
            except Exception:
                proc.kill()
                print(f"  [{name}] 强制终止")

    print(f"{Ansi.GREEN}所有服务已关闭{Ansi.RESET}")


# ============================================================================
# 模式 B：命令行实时监控
# ============================================================================

def _launch_live():
    """直接运行 pipeline.py 的主入口。"""
    from pipeline import main as pipeline_main
    try:
        asyncio.run(pipeline_main())
    except KeyboardInterrupt:
        print(f"\n{Ansi.GREEN}用户中断，程序退出{Ansi.RESET}")


# ============================================================================
# 模式 C：历史回测
# ============================================================================

def _launch_backtest(args):
    """运行回测沙盒。"""
    from run_backtest import run_backtest

    async def _run():
        await run_backtest(
            csv_path=args.csv_path,
            playback_speed=args.speed,
            use_mock_llm=args.mock_llm,
            use_db=args.db,
            use_trade=args.trade,
            quiet=args.quiet,
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print(f"\n{Ansi.GREEN}用户中断{Ansi.RESET}")


# ============================================================================
# 命令行解析
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Swap Momentum Tracker — 统一启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式:
  panel       启动可视化面板（FastAPI + Streamlit）
  live        启动命令行实时监控
  backtest    启动历史回测沙盒

示例:
  python launcher.py panel
  python launcher.py live
  python launcher.py backtest data/historical/TSLA-USDT-SWAP/2026-01-01.parquet --speed 10 --mock-llm
  python launcher.py backtest data/ticks.csv -s 0 --mock-llm --trade --db -q
        """,
    )

    sub = parser.add_subparsers(dest="mode", help="运行模式")

    # ── panel 模式 ──
    p_panel = sub.add_parser("panel", help="启动可视化面板")
    p_panel.add_argument("--host", default="0.0.0.0", help="API 监听地址")
    p_panel.add_argument("--port", type=int, default=8000, help="API 监听端口")

    # ── live 模式 ──
    sub.add_parser("live", help="启动命令行实时监控")

    # ── backtest 模式 ──
    p_bt = sub.add_parser("backtest", help="启动历史回测")
    p_bt.add_argument("csv_path", help="历史数据文件路径 (.csv 或 .parquet)")
    p_bt.add_argument("-s", "--speed", type=float, default=1.0,
                       help="回放速率（0=光速, 1=实时, 10=10倍速）")
    p_bt.add_argument("--mock-llm", action="store_true",
                       help="启用 Mock LLM（不调用真实 API）")
    p_bt.add_argument("--trade", "-t", action="store_true",
                       help="启用虚拟撮合交易")
    p_bt.add_argument("--db", "-d", action="store_true",
                       help="启用数据库持久化")
    p_bt.add_argument("--quiet", "-q", action="store_true",
                       help="静默模式")

    args = parser.parse_args()

    if args.mode is None:
        parser.print_help()
        print(f"\n{Ansi.YELLOW}请选择一种运行模式，例如: python launcher.py panel{Ansi.RESET}")
        return

    # ── 路由 ──
    if args.mode == "panel":
        _launch_panel(host=args.host, port=args.port)
    elif args.mode == "live":
        _launch_live()
    elif args.mode == "backtest":
        _launch_backtest(args)


if __name__ == "__main__":
    main()
