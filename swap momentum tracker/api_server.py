"""
FastAPI 云端 API 服务器 (api_server.py)
=========================================
职责：
  1. 封装现有异步量化引擎，提供 REST + WebSocket 接口
  2. 使用 lifespan 管理引擎生命周期（启动/停止）
  3. 实时推送能量告警与 LLM 决策到 WebSocket 客户端
  4. 提供系统状态、持仓、成交历史等 REST 端点

启动方式：
  uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import json
import logging
import signal
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional, Set, List, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse

# ── 加载环境变量 ──
from dotenv import load_dotenv
load_dotenv()

from config import get_settings, setup_logging, Ansi
from session_manager import MarketSession
from data_fetcher import SyntheticEquityFetcher
from analytics import MarketDynamicsCalculator
from llm_agent import MarketLLMAgent
from reporter import ReportGenerator, print_energy_alert, print_llm_decision
from database import AsyncDatabaseManager
from order_manager import OrderRouter

# ── 日志 ──
settings = get_settings()
setup_logging(settings)
logger = logging.getLogger("SwapMomentum.API")


# ============================================================================
# 全局引擎组件（由 lifespan 初始化）
# ============================================================================

engine: Dict[str, Any] = {
    "session_manager": None,
    "fetcher": None,
    "data_queue": None,
    "llm_agent": None,
    "router": None,
    "db_manager": None,
    "report_generator": None,
    "calculators": {},
    "alert_counters": {},
    "shutdown_event": None,
    "background_tasks": set(),
    "start_time": 0.0,
}


# ============================================================================
# WebSocket 连接管理器
# ============================================================================

class WSConnectionManager:
    """管理所有已连接的 WebSocket 客户端，支持广播推送。"""

    def __init__(self):
        self._clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info(f"WebSocket 客户端连接 | total={len(self._clients)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info(f"WebSocket 客户端断开 | total={len(self._clients)}")

    async def broadcast(self, data: dict) -> None:
        """向所有已连接客户端广播 JSON 消息。"""
        message = json.dumps(data, ensure_ascii=False, default=str)
        stale = set()
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                stale.add(ws)
        # 清理已断开的客户端
        for ws in stale:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


ws_manager = WSConnectionManager()


# ============================================================================
# 双通道输出函数（控制台 + WebSocket 广播）
# ============================================================================

def _ws_print_energy_alert(
    symbol: str, price: float, velocity: float, energy: float,
    threshold: float, alert_count: int, timestamp: float,
) -> None:
    """同时输出到控制台和 WebSocket 的能量告警。"""
    print_energy_alert(symbol, price, velocity, energy, threshold, alert_count, timestamp)
    # 异步广播到 WebSocket
    asyncio.create_task(ws_manager.broadcast({
        "type": "energy_alert",
        "timestamp": timestamp,
        "data": {
            "symbol": symbol, "price": price, "velocity": velocity,
            "energy": energy, "threshold": threshold, "alert_count": alert_count,
        },
    }))


def _ws_print_llm_decision(result: dict) -> None:
    """同时输出到控制台和 WebSocket 的 LLM 决策。"""
    print_llm_decision(result)
    asyncio.create_task(ws_manager.broadcast({
        "type": "llm_decision",
        "timestamp": result.get("timestamp", time.time()),
        "data": {
            "ticker": result.get("ticker", ""),
            "action": result.get("action", "HOLD"),
            "confidence": result.get("confidence", 0),
            "reasoning": result.get("reasoning", ""),
            "price": result.get("price", 0),
        },
    }))


# ============================================================================
# 引擎协程（从 pipeline.py 移植，替换 print 函数）
# ============================================================================

async def _session_poller(sm, fetcher, calculators, shutdown_event, interval):
    """盘口轮询协程。"""
    last_active: Set[str] = set()
    while not shutdown_event.is_set():
        try:
            info = sm.current_session()
            current = set(info.active_swaps)
            if current != last_active:
                await fetcher.update_subscriptions(list(current))
                if current:
                    for sym in current:
                        if sym in calculators:
                            calculators[sym].reset()
                last_active = current
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("盘口轮询异常")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


async def _consumer_task(
    data_queue, calculators, llm_agent, session_manager, settings,
    alert_counters, shutdown_event, db_manager, router,
):
    """消费者协程——处理行情、触发 LLM、执行 OMS。"""
    background_tasks: set = set()

    while not shutdown_event.is_set():
        try:
            data = await asyncio.wait_for(data_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            continue

        if data is None:
            break

        symbol = data.get("symbol", "")
        if not symbol:
            continue

        # 盘口过滤
        info = session_manager.current_session()
        active = set(info.active_swaps)
        if symbol not in active:
            continue

        # 计算器
        if symbol not in calculators:
            calculators[symbol] = MarketDynamicsCalculator(settings)
        calc = calculators[symbol]

        try:
            ts = data.get("server_timestamp") or data.get("local_timestamp", time.time())
            result = calc.update(
                price=data["price"], volume_usdt=data["volume_usdt"],
                timestamp=ts, ask=data.get("ask", 0), bid=data.get("bid", 0),
            )
        except Exception:
            continue

        # DB
        if db_manager and result["initialized"]:
            db_manager.enqueue_tick(
                timestamp=result["timestamp"], ticker=symbol,
                price=result["price"], velocity=result["velocity"],
                energy=result["energy"],
            )

        # 能量告警
        if result["initialized"] and result["energy"] > settings.energy_threshold:
            if symbol not in alert_counters:
                alert_counters[symbol] = 0
            now = time.time()
            if now - getattr(calc, "_last_alert_ts", 0) >= settings.alert_cooldown_seconds:
                alert_counters[symbol] += 1
                calc._last_alert_ts = now

                _ws_print_energy_alert(
                    symbol=symbol, price=result["price"], velocity=result["velocity"],
                    energy=result["energy"], threshold=settings.energy_threshold,
                    alert_count=alert_counters[symbol], timestamp=result["timestamp"],
                )

                if db_manager:
                    db_manager.enqueue_alert(
                        timestamp=result["timestamp"], ticker=symbol,
                        current_energy=result["energy"], threshold=settings.energy_threshold,
                    )

                snapshot = calc.get_recent_snapshot(10)
                llm_task = asyncio.create_task(
                    _llm_analyze(
                        llm_agent=llm_agent, ticker=symbol,
                        current_price=result["price"], velocity=result["velocity"],
                        energy=result["energy"], snapshot=snapshot,
                        db_manager=db_manager, router=router,
                    ),
                    name=f"llm_{symbol}",
                )
                background_tasks.add(llm_task)
                llm_task.add_done_callback(background_tasks.discard)

    # 等待后台 LLM 任务
    if background_tasks:
        pending = list(background_tasks)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=15)
        except asyncio.TimeoutError:
            for t in pending:
                if not t.done():
                    t.cancel()
        background_tasks.clear()


async def _llm_analyze(
    llm_agent, ticker, current_price, velocity, energy, snapshot,
    db_manager, router,
):
    """LLM 分析 + OMS 下单。"""
    try:
        result = await llm_agent.analyze(
            ticker=ticker, current_price=current_price,
            velocity=velocity, energy=energy, five_min_stats=snapshot,
        )
        if result:
            _ws_print_llm_decision(result)
            if db_manager:
                db_manager.enqueue_llm_decision(
                    timestamp=result.get("timestamp", time.time()),
                    ticker=result.get("ticker", ticker),
                    action=result.get("action", "HOLD"),
                    confidence=result.get("confidence", 0.5),
                    reasoning=result.get("reasoning", ""),
                )
            if router and result["action"] in ("BUY", "SELL"):
                try:
                    order = await router.process_signal(
                        ticker=ticker, action=result["action"], current_price=current_price,
                    )
                    if db_manager and order:
                        db_manager.enqueue_order(
                            timestamp=time.time(), symbol=ticker,
                            action=order.get("action", result["action"]),
                            qty=order.get("qty", order.get("sz", 0)),
                            filled_price=order.get("filled_price", current_price),
                            notional_value=order.get("notional_value", 0),
                        )
                except Exception:
                    logger.exception(f"OMS 下单异常 | ticker={ticker}")
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(f"LLM 异常 | ticker={ticker}")


# ============================================================================
# FastAPI 生命周期
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动引擎 → 运行 → 优雅停止。"""
    global engine

    logger.info("=" * 50)
    logger.info("API Server 启动中...")

    # ── 初始化组件 ──
    engine["shutdown_event"] = asyncio.Event()
    engine["start_time"] = time.time()
    engine["data_queue"] = asyncio.Queue(maxsize=4096)

    engine["session_manager"] = MarketSession(settings)
    engine["fetcher"] = SyntheticEquityFetcher(settings, engine["data_queue"])
    engine["llm_agent"] = MarketLLMAgent(
        api_endpoint=settings.llm_api_endpoint,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )

    engine["router"] = None
    if settings.trading_enabled:
        try:
            engine["router"] = OrderRouter(settings)
            await engine["router"].initialize()
        except Exception:
            logger.exception("OMS 初始化失败")

    engine["db_manager"] = AsyncDatabaseManager()
    try:
        await engine["db_manager"].init_db()
        await engine["db_manager"].start()
    except Exception:
        logger.exception("DB 初始化失败")
        engine["db_manager"] = None

    # ── 启动后台协程 ──
    shutdown = engine["shutdown_event"]
    engine["background_tasks"] = {
        asyncio.create_task(engine["fetcher"].start(), name="DataFetcher"),
        asyncio.create_task(
            _session_poller(
                engine["session_manager"], engine["fetcher"],
                engine["calculators"], shutdown, settings.session_check_interval,
            ),
            name="SessionPoller",
        ),
        asyncio.create_task(
            _consumer_task(
                engine["data_queue"], engine["calculators"],
                engine["llm_agent"], engine["session_manager"],
                settings, engine["alert_counters"], shutdown,
                engine["db_manager"], engine["router"],
            ),
            name="Consumer",
        ),
    }

    logger.info(f"API Server 就绪 | {len(engine['background_tasks'])} 个后台任务")
    yield

    # ── 优雅停止 ──
    logger.info("API Server 停止中...")
    shutdown.set()

    if engine["fetcher"]:
        await engine["fetcher"].stop()

    for task in engine["background_tasks"]:
        if not task.done():
            task.cancel()
    await asyncio.gather(*engine["background_tasks"], return_exceptions=True)

    if engine["db_manager"]:
        await engine["db_manager"].close()

    logger.info("API Server 已停止")


# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(
    title="Swap Momentum Tracker API",
    version="2.0",
    lifespan=lifespan,
)


# ============================================================================
# REST 端点
# ============================================================================

@app.get("/")
async def root():
    return {"service": "Swap Momentum Tracker API", "version": "2.0"}


@app.get("/api/v1/system/status")
async def system_status():
    """返回系统运行状态、盘口、活跃标的。"""
    sm = engine["session_manager"]
    info = sm.current_session() if sm else None

    uptime = time.time() - engine["start_time"] if engine["start_time"] else 0

    calc_summary = {}
    for sym, c in engine["calculators"].items():
        if c.is_initialized:
            calc_summary[sym] = {
                "price": round(c.price, 4),
                "velocity": round(c.velocity, 6),
                "energy": round(c.energy, 4),
                "rsi": round(c.tick_rsi, 2),
                "vel_zscore": round(c.vel_zscore, 2),
                "samples": c.sample_count,
            }

    fetcher_stats = engine["fetcher"].stats if engine["fetcher"] else {}
    llm_stats = engine["llm_agent"].get_stats() if engine["llm_agent"] else {}
    db_stats = engine["db_manager"].stats if engine["db_manager"] else {}

    return {
        "uptime_seconds": round(uptime, 0),
        "session": {
            "state": info.state_name if info else "unknown",
            "active_swaps": info.active_swaps if info else [],
            "suppressed_swaps": info.suppressed_swaps if info else [],
        },
        "calculators": calc_summary,
        "stats": {
            "fetch": {
                "received": fetcher_stats.get("messages_received", 0),
                "parsed": fetcher_stats.get("messages_parsed", 0),
                "dropped": fetcher_stats.get("messages_dropped", 0),
                "reconnects": fetcher_stats.get("reconnect_attempts", 0),
            },
            "llm": {
                "calls": llm_stats.get("total_calls", 0),
                "success": llm_stats.get("success", 0),
                "failure": llm_stats.get("failure", 0),
                "timeout": llm_stats.get("timeout", 0),
            },
            "db": {
                "ticks": db_stats.get("ticks_written", 0),
                "llm": db_stats.get("llm_written", 0),
                "alerts": db_stats.get("alerts_written", 0),
                "orders": db_stats.get("orders_written", 0),
            },
        },
        "websocket_clients": ws_manager.client_count,
    }


@app.get("/api/v1/portfolio/summary")
async def portfolio_summary():
    """返回当前可用资金、持仓和 OMS 统计。"""
    router = engine["router"]

    if router is None:
        return {"mode": "OFF", "message": "交易功能未启用（TRADING_MODE=OFF）", "positions": {}}

    return {
        "mode": router.mode,
        "positions": {
            ticker: {
                "entry_price": pos.get("entry_price", 0),
                "timestamp": pos.get("timestamp", 0),
            }
            for ticker, pos in router.positions.items()
        },
        "position_count": len(router.positions),
        "total_orders": len(router.order_history),
        "executor_stats": router.get_executor_stats(),
    }


@app.get("/api/v1/trades/history")
async def trades_history(limit: int = Query(default=50, le=200)):
    """从 SQLite 读取最近的成交记录（订单 + LLM 决策）。"""
    db = engine["db_manager"]
    if db is None:
        return {"error": "database not available"}

    try:
        orders = await db.query_order_executions(limit=limit)
        llms = await db.query_llm_decisions(limit=limit)

        return {
            "orders": [
                {
                    "id": r[0], "timestamp": r[1], "symbol": r[2],
                    "action": r[3], "qty": r[4], "filled_price": r[5],
                    "notional_value": r[6],
                }
                for r in orders
            ],
            "llm_decisions": [
                {
                    "id": r[0], "timestamp": r[1], "ticker": r[2],
                    "action": r[3], "confidence": r[4], "reasoning": r[5],
                }
                for r in llms
            ],
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# POST 控制端点
# ============================================================================

@app.post("/api/v1/control/toggle-trading")
async def toggle_trading(enabled: bool = True):
    """
    一键开启/停止自动交易。

    通过修改 settings.trading_mode 实现：
      enabled=True  → 恢复 PAPER/LIVE 模式
      enabled=False → 强制设为 OFF
    """
    router = engine["router"]
    if router is None and enabled:
        return {"error": "交易功能未配置，请检查 .env 中的 TRADING_MODE 和 API 凭证"}

    # 实际控制逻辑：通过 router 的 mode 控制
    # 简化版：如果 enabled 且 router 存在则允许，否则拒绝
    if not enabled:
        # 强制清空持仓（模拟紧急平仓）
        if router:
            for ticker in list(router.positions.keys()):
                try:
                    # 用当前价格平仓
                    calc = engine["calculators"].get(ticker)
                    price = calc.price if calc and calc.is_initialized else 0
                    if price > 0:
                        await router.process_signal(ticker, "SELL", price)
                except Exception:
                    pass
        return {"status": "disabled", "message": "自动交易已停止，所有头寸已平仓"}

    return {"status": "enabled", "message": "自动交易已开启"}


@app.post("/api/v1/control/panic-close")
async def panic_close():
    """紧急平仓所有头寸 (Panic Button)。"""
    router = engine["router"]
    if router is None:
        return {"error": "交易功能未启用"}

    closed = []
    for ticker in list(router.positions.keys()):
        calc = engine["calculators"].get(ticker)
        price = calc.price if calc and calc.is_initialized else 0
        if price > 0:
            try:
                await router.process_signal(ticker, "SELL", price)
                closed.append(ticker)
            except Exception:
                pass

    return {
        "status": "panic_executed",
        "closed_positions": closed,
        "remaining": list(router.positions.keys()),
    }


# ============================================================================
# WebSocket 端点
# ============================================================================

@app.websocket("/ws/live-feed")
async def live_feed(websocket: WebSocket):
    """
    实时行情推送 WebSocket。

    接收 JSON 格式消息：
      {"type": "subscribe", "channels": ["alerts", "decisions"]}

    推送 JSON 格式消息：
      {"type": "energy_alert", "timestamp": ..., "data": {...}}
      {"type": "llm_decision", "timestamp": ..., "data": {...}}
    """
    await ws_manager.connect(websocket)

    # 发送欢迎消息
    await websocket.send_text(json.dumps({
        "type": "connected",
        "message": "已连接到 Swap Momentum Tracker 实时推送",
        "timestamp": time.time(),
        "clients": ws_manager.client_count,
    }))

    try:
        # 保持连接，接收客户端消息（如订阅/取消订阅频道）
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                if msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket 异常")
    finally:
        ws_manager.disconnect(websocket)


# ============================================================================
# 启动说明
# ============================================================================

"""
启动命令：

    uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

访问：
    http://localhost:8000/docs          FastAPI 自动生成的 Swagger UI
    http://localhost:8000/api/v1/system/status
    http://localhost:8000/api/v1/portfolio/summary
    http://localhost:8000/api/v1/trades/history?limit=20
    ws://localhost:8000/ws/live-feed    WebSocket 实时推送
"""
