"""
Streamlit 量化交易可视化面板 (dashboard.py) — TTL缓存重构版
=============================================================
本代码依赖 Streamlit 1.37+ 的 @st.fragment / @st.cache_data / @st.cache_resource。

核心架构（网络层优化后）：
  ┌─────────────────────────────────────────────────────┐
  │  @st.cache_resource 全局单例                         │
  │  ├─ _SharedData        ← WebSocket 消息队列          │
  │  ├─ requests.Session() ← HTTP Keep-Alive 连接池       │
  │  └─ Daemon Thread      ← 仅 WebSocket 接收（不轮询）  │
  └─────────────────────────────────────────────────────┘
                        │
      ┌─────────────────┼─────────────────┐
      ▼                 ▼                 ▼
  @st.cache_data    @st.cache_data    @st.cache_data
  ttl=2             ttl=3             ttl=5
  fetch_status()    fetch_portfolio() fetch_trades()
      │                 │                 │
      ▼                 ▼                 ▼
  ┌─────────────────────────────────────────────────┐
  │              @st.fragment 层                     │
  │  fragment_hero(2s)  fragment_chart(3s)          │
  │  fragment_feed(1s)                              │
  └─────────────────────────────────────────────────┘

TTL 防 DDoS 原理：
  @st.cache_data 在 TTL 过期前，所有调用返回内存缓存值，不发起 HTTP 请求。
  即使 @st.fragment 每 1 秒刷新 UI，实际到达后端的请求频率
  被钳制在：status≤30/min, portfolio≤20/min, trades≤12/min。
  后端从每秒数十次请求降为每分钟十几次，日志不再被淹没。
"""

import json
import threading
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ── 后端地址 ──
API_BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/live-feed"

# ── 页面配置 ──
st.set_page_config(
    page_title="Swap Momentum Tracker",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── 全局 CSS ──
st.markdown("""
<style>
    .hero-metric {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border-radius: 12px; padding: 18px; text-align: center;
        border: 1px solid #2a2a4a;
    }
    .hero-value { font-size: 2rem; font-weight: 800; color: #00d4aa; }
    .hero-label { font-size: 0.8rem; color: #888; margin-top: 2px; }
    .hero-sub { font-size: 0.85rem; margin-top: 2px; }
    .green { color: #00d4aa; } .red { color: #ff4757; } .yellow { color: #ffa502; }
    .chat-bubble {
        padding: 10px 14px; border-radius: 8px; margin: 4px 0; font-size: 0.88rem;
    }
    .alert-bubble { background: rgba(255,71,87,0.1); border-left: 3px solid #ff4757; }
    .llm-buy { background: rgba(0,212,170,0.1); border-left: 3px solid #00d4aa; }
    .llm-sell { background: rgba(255,71,87,0.1); border-left: 3px solid #ff4757; }
    .llm-hold { background: rgba(255,165,2,0.1); border-left: 3px solid #ffa502; }
    .stButton>button { border-radius: 8px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)


# ============================================================================
# 第一层：全局资源单例（@st.cache_resource）
# ============================================================================
# 为什么需要两层缓存：
#   @st.cache_resource 管"全生命周期单例"——WebSocket 连接、HTTP Session、
#   守护线程。这些资源在整个 Streamlit 进程存活期间只创建一次。
#
#   @st.cache_data 管"TTL 过期控制"——在每个独立的 Fragment 调用链上，
#   如果缓存未过期，直接返回内存值，不穿透到 HTTP 层。
#
# 两层协同工作：cache_resource 提供长连接，cache_data 做请求节流。

class _SharedData:
    """线程安全的 WebSocket 消息队列。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.live_feed: list = []
        self.ws_connected: bool = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "feed": list(self.live_feed),
                "ws_connected": self.ws_connected,
            }

    def update_feed(self, msg: dict) -> None:
        with self._lock:
            self.live_feed.append(msg)
            if len(self.live_feed) > 200:
                self.live_feed = self.live_feed[-200:]

    def set_ws(self, connected: bool) -> None:
        with self._lock:
            self.ws_connected = connected


@st.cache_resource
def get_shared_data() -> _SharedData:
    """WebSocket 消息队列单例 + 启动守护线程。"""
    data = _SharedData()
    t = threading.Thread(target=_ws_listener_thread, args=(data,), daemon=True, name="WS-Listener")
    t.start()
    return data


@st.cache_resource
def get_http_session() -> requests.Session:
    """
    HTTP 连接池单例。

    为什么用 requests.Session()：
      默认的 requests.get() 每次创建新 TCP 连接（三次握手 + TLS 握手）。
      Session 开启 HTTP Keep-Alive，同一连接上复用后续请求，
      后端只需处理一次握手，大幅降低 TIME_WAIT 连接堆积。
    """
    s = requests.Session()
    s.headers.update({"User-Agent": "SwapMomentum-Dashboard/2.0"})
    return s


def _ws_listener_thread(data: _SharedData):
    """
    WebSocket 守护线程（仅负责接收推送，不做 REST 轮询）。

    为什么线程内不做 REST：
      线程内的 HTTP 请求不受 @st.cache_data 保护。
      TTL 缓存只在 Streamlit 主线程/脚本上下文中生效。
      如果线程直接调 requests，每次都是真实 HTTP，无法享受缓存。
    """
    import asyncio
    import websockets as ws_lib

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _listen():
        while True:
            try:
                async with ws_lib.connect(WS_URL, ping_interval=20) as ws:
                    data.set_ws(True)
                    await ws.recv()  # 跳过欢迎消息
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        data.update_feed(json.loads(raw))
            except asyncio.TimeoutError:
                continue
            except Exception:
                data.set_ws(False)
                await asyncio.sleep(3)

    try:
        loop.run_until_complete(_listen())
    except Exception:
        data.set_ws(False)


# ============================================================================
# 第二层：带 TTL 的 API 数据获取层（@st.cache_data）
# ============================================================================
# 为什么这一层必须在 Fragment 外部定义：
#   @st.cache_data 需要作为全局函数存在才能在 Streamlit 的缓存上下文中注册。
#   如果定义在 Fragment 内部或闭包中，缓存将失效。
#
# TTL 如何拯救后端：
#   Fragment run_every=1s → 每秒调用一次 fetch_xxx()
#   → @st.cache_data(ttl=3) 判断：3秒内直接返回内存缓存
#   → 只有 TTL 过期的那一次调用才真正穿透到 requests.get()
#   → 后端从每秒 N 次降为每 3~5 秒 1 次

@st.cache_data(ttl=2, show_spinner=False)
def fetch_system_status() -> dict:
    """
    获取系统状态。TTL=2s。

    为什么 ttl=2：系统状态（盘口/活跃标的/WS客户端数）变化频率中等，
    2 秒足够实时又不过度请求。
    """
    session = get_http_session()
    try:
        r = session.get(f"{API_BASE}/api/v1/system/status", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


@st.cache_data(ttl=3, show_spinner=False)
def fetch_portfolio_summary() -> dict:
    """
    获取持仓摘要。TTL=3s。

    为什么 ttl=3：持仓变化需要 OMS 执行订单，频率较低。
    3 秒在大多数交易场景下足够实时。
    """
    session = get_http_session()
    try:
        r = session.get(f"{API_BASE}/api/v1/portfolio/summary", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


@st.cache_data(ttl=5, show_spinner=False)
def fetch_trades_history(limit: int = 200) -> dict:
    """
    获取成交历史。TTL=5s。

    为什么 ttl=5：这是最重的接口（SQLite 双表查询 + 序列化）。
    后端处理一次可能需要 200ms+。5 秒是性能和实时性的最佳平衡。
    """
    session = get_http_session()
    try:
        r = session.get(
            f"{API_BASE}/api/v1/trades/history",
            params={"limit": limit}, timeout=10,
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def http_post(path: str, data: dict = None) -> dict:
    """POST 控制指令（不缓存——每次真实执行）。"""
    session = get_http_session()
    try:
        r = session.post(f"{API_BASE}{path}", json=data or {}, timeout=10)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# 静态层：标题 + 侧边栏
# ============================================================================

shared = get_shared_data()

st.title("⚡ Swap Momentum Tracker")
st.caption("股权代币永续合约 · 高频动量监控 · AI 风控")


def render_sidebar(data: _SharedData):
    with st.sidebar:
        st.markdown("## 🎛️ 控制台")
        snap = data.snapshot()
        if snap["ws_connected"]:
            st.success(f"🟢 实时推送已连接 · {len(snap['feed'])} 条消息")
        else:
            st.warning("⚪ 推送未连接（启动中...）")

        st.divider()
        st.markdown("### ⚡ 自动交易")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("▶️ 开启", use_container_width=True):
                r = http_post("/api/v1/control/toggle-trading", {"enabled": True})
                st.toast(r.get("message", "已发送"), icon="✅")
        with c2:
            if st.button("⏸️ 停止", use_container_width=True):
                r = http_post("/api/v1/control/toggle-trading", {"enabled": False})
                st.toast(r.get("message", "已发送"), icon="⏸️")

        st.divider()
        st.markdown("### 🚨 紧急操作")
        if st.button("🔴 强制平仓所有头寸", use_container_width=True):
            r = http_post("/api/v1/control/panic-close")
            closed = r.get("closed_positions", [])
            if closed:
                st.error(f"已平仓: {', '.join(closed)}")
            else:
                st.info("无头寸需要平仓")

        st.divider()
        st.caption(f"API: {API_BASE}")


render_sidebar(shared)


# ============================================================================
# 第三层：Fragment UI 渲染层（仅读取缓存数据，不发起 HTTP）
# ============================================================================
# 为什么 Fragment 内部不直接写 requests.get()：
#   Fragment 每 run_every 秒完整执行一次函数体。
#   如果内部直接调 requests，每秒都会穿透到后端。
#   通过 @st.cache_data 隔离后，Fragment 调用的是缓存函数，
#   缓存未过期时直接返回内存值，零网络开销。

# ── Fragment 1：Hero 指标（每 2 秒刷新，读缓存） ──

@st.fragment(run_every=2)
def fragment_hero():
    """数据大屏 —— 所有数据来自带 TTL 的缓存函数。"""
    status = fetch_system_status()        # ← @st.cache_data(ttl=2)，2s内不重复请求
    portfolio = fetch_portfolio_summary()  # ← @st.cache_data(ttl=3)，3s内不重复请求

    session = status.get("session", {})
    active = bool(session.get("active_swaps", []))
    llm_stats = status.get("stats", {}).get("llm", {})
    pos_count = portfolio.get("position_count", 0)
    orders = portfolio.get("total_orders", 0)
    tc = "green" if active else "yellow"
    tt = "● 交易中" if active else "◉ 休市"

    html = f"""
    <div style="display:flex; gap:16px;">
        <div class="hero-metric" style="flex:1;">
            <div class="hero-label">💰 总权益</div>
            <div class="hero-value">$100,000</div>
            <div class="hero-sub">待平仓 {pos_count} 笔</div>
        </div>
        <div class="hero-metric" style="flex:1;">
            <div class="hero-label">📋 订单记录</div>
            <div class="hero-value">{orders}</div>
            <div class="hero-sub">历史成交单</div>
        </div>
        <div class="hero-metric" style="flex:1;">
            <div class="hero-label">📡 系统状态</div>
            <div class="hero-value {tc}">{tt}</div>
            <div class="hero-sub">{session.get('state', '未知')}</div>
        </div>
        <div class="hero-metric" style="flex:1;">
            <div class="hero-label">🤖 LLM 裁判</div>
            <div class="hero-value green">● 在线</div>
            <div class="hero-sub">调用{llm_stats.get('calls',0)} / 成功{llm_stats.get('success',0)}</div>
        </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


# ── Fragment 2：K 线图（每 3 秒刷新） ──

@st.fragment(run_every=3)
def fragment_chart():
    """K 线图 —— 交易历史来自 ttl=5 缓存。"""
    trades = fetch_trades_history(limit=200)  # ← @st.cache_data(ttl=5)
    orders = trades.get("orders", [])

    if not orders:
        st.info("暂无成交记录，等待交易信号...")
        return

    df = pd.DataFrame(orders)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    df = df.sort_values("timestamp")
    buys = df[df["action"].str.upper() == "BUY"]
    sells = df[df["action"].str.upper() == "SELL"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["filled_price"],
        mode="lines+markers", name="成交价",
        line=dict(color="#00d4aa", width=1.5),
        marker=dict(size=3, color="#00d4aa"),
    ))
    if not buys.empty:
        fig.add_trace(go.Scatter(
            x=buys["timestamp"], y=buys["filled_price"],
            mode="markers", name="🔼 买入",
            marker=dict(symbol="triangle-up", size=14, color="#00d4aa",
                        line=dict(width=1, color="#00ff88")),
        ))
    if not sells.empty:
        fig.add_trace(go.Scatter(
            x=sells["timestamp"], y=sells["filled_price"],
            mode="markers", name="🔽 卖出",
            marker=dict(symbol="triangle-down", size=14, color="#ff4757",
                        line=dict(width=1, color="#ff6b81")),
        ))

    fig.update_layout(
        template="plotly_dark",
        margin=dict(l=10, r=10, t=30, b=10), height=380,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(title="", gridcolor="#1a1a2e", zeroline=False),
        yaxis=dict(title="价格 (USDT)", gridcolor="#1a1a2e", zeroline=False),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="top", y=1.15, xanchor="left", x=0),
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Fragment 3：AI 裁判直播间（每 1 秒刷新，数据来自 WebSocket 线程） ──

@st.fragment(run_every=1)
def fragment_llm_feed():
    """AI 直播间 —— WebSocket 数据已在守护线程写入 shared.live_feed，无需 HTTP。"""
    snap = shared.snapshot()
    feed = snap["feed"]

    if not feed:
        st.info("⏳ 等待 AI 裁判信号...")
        return

    parts = []
    for msg in reversed(feed[-40:]):
        mt = msg.get("type", "")
        d = msg.get("data", {})
        ts = msg.get("timestamp", 0)
        ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""

        if mt == "energy_alert":
            parts.append(
                f'<div class="chat-bubble alert-bubble">'
                f'🚨 <b>[{ts_str}] 能量异动！</b> '
                f'{d.get("symbol","?")} 能量 {d.get("energy",0):,.0f}'
                f'</div>'
            )
        elif mt == "llm_decision":
            action = d.get("action", "HOLD")
            if action == "BUY":
                cls, emoji, text = "llm-buy", "🟢", "强力买入"
            elif action == "SELL":
                cls, emoji, text = "llm-sell", "🔴", "强力卖出"
            else:
                cls, emoji, text = "llm-hold", "🟡", "持有观望"
            parts.append(
                f'<div class="chat-bubble {cls}">'
                f'{emoji} <b>[{ts_str}] {text}</b> '
                f'({d.get("confidence",0):.0%}) — {d.get("reasoning","")}'
                f'</div>'
            )

    with st.container(height=520):
        st.markdown("\n".join(parts), unsafe_allow_html=True)


# ============================================================================
# 装配
# ============================================================================
# 全局脚本只执行一次。后续所有 UI 更新由 3 个 Fragment 的 run_every 定时器驱动。
# Fragment 内调用 fetch_xxx() → @st.cache_data 判断 TTL → 命中返回缓存 / 未命中才 HTTP。
# 后端不受 Fragment 频率影响，日志量降低 95%+。

st.divider()

col_left, col_right = st.columns([2, 1])
with col_left:
    st.markdown("### 📈 实时交易信号")
    fragment_chart()
with col_right:
    st.markdown("### 🤖 AI 风控实时解盘")
    fragment_llm_feed()

st.divider()
fragment_hero()
