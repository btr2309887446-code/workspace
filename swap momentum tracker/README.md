<p align="center">
  <h1 align="center">⚡ Synthetic Equity Swap Momentum Tracker</h1>
  <p align="center"><b>股权代币永续合约 · 动量实时监控与自动交易系统</b></p>
  <p align="center">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python">
    <img alt="Async" src="https://img.shields.io/badge/Async-asyncio-orange">
    <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
    <img alt="Status" src="https://img.shields.io/badge/Status-Production%20Ready-brightgreen">
    <img alt="SQLite" src="https://img.shields.io/badge/DB-SQLite%20(aiosqlite)-lightgrey">
  </p>
</p>

---

## 📖 项目简介

Synthetic Equity Swap Momentum Tracker 是一套**纯 Python 异步高频量化风控与自动交易系统**。它通过连接 **OKX V5 交易所公共 WebSocket**，实时监控底层锚定传统美股与韩股的 USDT 本位永续合约（如 `TSLA-USDT-SWAP`、`AAPL-USDT-SWAP`、`SAMSUNG-USDT-SWAP`）。

系统引入**物理学概念**——将价格变动抽象为**速度（Velocity）**与**能量（Energy）**，通过 EMA 平滑滤波与成交量加权，精准捕捉多空博弈中的真实资金强度。当瞬时能量突破阈值时，系统**异步唤醒 LLM 大模型**作为"风控裁判大脑"判断信号真伪，并可通过**策略模式订单管理系统（OMS）**自动执行交易。

> 🎯 **核心痛点解决：** 传统股市波动率指标在 24×7 加密市场中失效。加密永续合约即使底层现货休市仍有零星成交，但这些"伪动能"会误导量化策略。本系统通过**时区感知的盘口状态机**精准过滤非交易时段的垃圾信号，用 LLM 的推理能力替代硬编码规则，实现从**信号识别 → 风控判决 → 自动下单**的完整闭环。

---

## ✨ 核心特性

### ⚡ 极高频纯异步架构
- 基于 `asyncio` + `websockets` + `aiohttp` 的完全异步事件循环
- 5 个独立协程并发运行（数据获取 / 盘口调度 / 消费计算 / 周期报告 / 状态打印）
- OKX WebSocket 实时推送，延迟 < 100ms，24×7 不间断运行
- 所有 I/O（LLM API、数据库、订单执行）全程异步，**绝不阻塞主行情流**

### 🧮 物理动能微积分算法
- **Velocity (速度):** 价格一阶导数经 EMA 指数平滑 `v_t = α · (ΔP/Δt) + (1-α) · v_{t-1}`
- **Energy (能量):** 成交量加权动能 `E = |v| × Volume`，模拟物理动能公式
- **能量积分:** 梯形法则近似 `∫E dt ≈ Σ (E_i + E_{i-1}) / 2 × Δt_i`
- **开盘跳空检测：** 跨长时间间隔（>300s）自动跳过速度计算，防止 EMA 被污染
- 环形缓冲区 (`collections.deque`)，O(1) 追加，自动淘汰历史数据，**零内存泄漏**

### 🌍 智能时区感知盘口状态机
- 基于 `pytz` 的精确时区转换（UTC ↔ KST ↔ US Eastern，自动处理夏/冬令时）
- **独立判定**：首尔时间与美东时间各自判断周末，杜绝跨时区误伤
- 覆盖 5 种盘口状态：韩股交易 / 美股盘前 / 美股盘中 / 美股盘后 / 全休市
- 休市期间**主动取消 WebSocket 订阅 + 过滤残存数据**，双重保险防伪动能

### 🧠 LLM 风控裁判大脑
- 支持 **OpenAI 兼容格式**（ChatGPT / DeepSeek / OpenClaw / Ollama / vLLM）与 **Google Gemini API** 双后端，根据 endpoint URL 自动路由
- 超时熔断（10s Circuit Breaker）——超时直接返回 `None`，不拖累主循环
- 5 层 JSON 解析容错：剥离 Markdown → 提取花括号 → json.loads → 单引号修复 → Key 加引号
- **中/英文字段双通道回退**：`action`←`动作`/`操作`/`决策`、`confidence`←`置信度`/`概率`/`把握`、`reasoning`←`理由`/`逻辑`/`分析`/`原因`
- 每标的 60 秒冷却期，防止连续刷屏

### 📊 订单管理系统 (OMS)
- **策略模式架构**：`BaseExecutor` 抽象基类 → `AlpacaPaperExecutor` / `OKXLiveExecutor` 无缝切换
- **名义价值驱动下单**：统一以 USD 名义价值为基准，Alpaca 折算碎股、OKX 折算整数合约张数
- **动态合约面值缓存**：OKX 模式下自动拉取 `GET /api/v5/public/instruments` 缓存 `ctVal`
- **单向做多互斥**：BUY+无仓→开多 / BUY+有仓→忽略 / SELL+有仓→平多 / SELL+无仓→忽略
- **手动 HMAC SHA256 签名**：纯 Python 实现 OKX V5 私有接口鉴权
- `TRADING_MODE=OFF` 时完全跳过下单，可安全用于只读回测

### 💾 时序数据库持久化
- 基于 `aiosqlite` 的异步 SQLite，**零阻塞**主事件循环
- 3 张核心表：`ticks_history`（瞬时状态）、`llm_decisions`（风控日志）、`energy_alerts`（告警事件）
- **异步批量刷写机制：** 内部缓冲区 + 后台协程定时/定量（满 50 条或超 3 秒）触发 `executemany` 事务写入
- 优雅停机时强制刷盘残余数据，**零数据丢失**

### 🔄 历史回测沙盒
- `HistoricalCSVFeeder` 接口与实盘 `SyntheticEquityFetcher` 完全一致，实现**无缝依赖注入**
- 支持 **光速回放**（speed=0，全速）与 **仿真回放**（speed=N，sleep(Δt/N)）
- 独立启动器 `run_backtest.py`，支持 Mock LLM 模式（零 API 成本验证策略）
- 回测默认**跳过盘口过滤**，确保 CSV 每一行都被连续计算

### 🛡️ 生产级容错与优雅停机
- 所有协程 `try-except Exception` 严密包裹，单 Tick 损坏不崩溃
- 指数退避无限重连（含随机抖动 ∆±50%，防惊群效应）
- WebSocket 协议级 + OKX 应用层 `"ping"`/`"pong"` 双层心跳保活
- `asyncio.Task` 强引用集合 + `add_done_callback` 防 GC 意外回收
- 停机时等待所有后台 LLM 任务完成后才关闭 DB，杜绝数据竞态
- OMS 所有 API 调用带 `try-except` 捕获余额不足/限流/超时，绝不崩溃主循环
- `SIGINT`/`SIGTERM` 信号捕获 → 安全关闭连接 → 有序退出

---

## 🏗 系统架构与模块说明

```
swap momentum tracker/
├── config.py              # 配置中心（标的池/端点/密钥/阈值/日志/交易模式）
├── session_manager.py     # 时区状态机（pytz KST/ET 转换 + 独立周末判定）
├── data_fetcher.py        # 实时数据源（OKX WebSocket 订阅/重连/双层心跳）
├── analytics.py           # 量化核心（Velocity/Energy/能量积分/跳空检测）
├── llm_agent.py           # LLM 风控裁判（OpenAI+Gemini 双后端/熔断/JSON 解析）
├── order_manager.py       # 订单管理系统（策略模式/Alpaca+OKX/做多互斥）
├── reporter.py            # 报告模块（彩色告警打印 + 5分钟 .txt 报告）
├── database.py            # 时序数据库（aiosqlite + 批量刷写 + 停机刷盘）
├── backtest_feeder.py     # 历史回放源（CSV 解析 / 光速回放 / 仿真速率控制）
├── run_backtest.py        # 回测启动器（沙盒模式 / Mock LLM / 跳过盘口过滤）
├── pipeline.py            # 主调度引擎（5 协程并发 + 事件驱动 LLM + 优雅停机）
├── reports/               # 周期报告输出目录
├── logs/                  # 轮转日志目录
├── data/                  # SQLite 数据库文件目录（自动创建）
│   └── quant_memory.db
└── .env                   # 环境变量配置（可选）
```

### 模块职责与数据流

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `config.py` | 集中管理所有可调参数、API 密钥、交易模式 | 环境变量 / `.env` | `Settings` 实例 |
| `session_manager.py` | 判定 KST/ET 盘口，返回活跃标的 | UTC 时间 | `SessionInfo` |
| `data_fetcher.py` | OKX WebSocket 订阅 Ticker，解析 JSON | 活跃标的列表 | `asyncio.Queue` |
| `analytics.py` | 环形缓冲区 + 速度/能量/跳空检测 | 行情数据 | 指标快照 + 窗口聚合 |
| `llm_agent.py` | 异步调用 LLM API，多后端路由，JSON 解析 | 量价特征 + 5分钟统计 | `{action, confidence, reasoning}` |
| `order_manager.py` | 策略模式 OMS：名义价值驱动 + 做多互斥 | LLM 判决 + 价格 | 订单结果 |
| `reporter.py` | 彩色告警 + 5 分钟 `.txt` 报告 | 计算器 + Fetcher | 控制台 + 文件 |
| `database.py` | 异步批量写入 3 张表，停机刷盘 | 入队调用 | SQLite 持久化 |
| `backtest_feeder.py` | CSV 回放入队，接口兼容实盘 Fetcher | CSV 文件 | `asyncio.Queue` |
| `run_backtest.py` | 独立回测入口，依赖注入 + 跳过盘口过滤 | CLI 参数 | 回测摘要 |
| `pipeline.py` | 5 协程并发调度 + 事件驱动 LLM + 优雅停机 | Settings | 系统运行 |

```
                          ┌─────────────────────┐
                          │     config.py        │ ◀── 环境变量 / .env
                          └────────┬────────────┘
                                   │
    ┌──────────────┐  ┌────────────┴────────────┐  ┌──────────────────┐
    │SessionManager│  │     pipeline.py          │  │   LLMAgent       │
    │ 盘口/周末判定 │  │  ┌───────────────────┐   │  │  OpenAI/Gemini   │
    └──────┬───────┘  │  │ SessionPoller     │   │  │  超时熔断        │
           │          │  │ → DataFetcher     │   │  └────────▲─────────┘
           │          │  └────────┬──────────┘   │           │
           │          │           │ Queue         │           │
           │          │  ┌────────▼──────────┐   │  ┌────────┴─────────┐
           │          │  │ Consumer          │   │  │ Energy Alert     │
           │          │  │ ├─ Filter by ses. │   │  │ > threshold?     │
           │          │  │ ├─ Calc.update()  │───│──│ create_task(llm) │
           │          │  │ ├─ DB.enqueue()   │   │  └────────┬─────────┘
           │          │  │ └─ Alert check    │   │           │
           │          │  └────────┬──────────┘   │           │ BUY/SELL
           │          │           │              │           ▼
           │          │  ┌────────▼──────────┐   │  ┌──────────────────┐
           │          │  │ AsyncDatabaseMgr │   │  │  OrderRouter     │
           │          │  │ ticks/llm/alerts │   │  │  Long-Only Mutex │
           │          │  └──────────────────┘   │  │  Alpaca | OKX    │
           │          │                         │  └──────────────────┘
           │          │  ┌──────────────────┐   │  ┌──────────────────┐
           │          │  │ PeriodicReporter │   │  │  StatsPrinter    │
           │          │  │ 5-min .txt report│   │  │  120s status     │
           │          │  └──────────────────┘   │  └──────────────────┘
           │          └──────────────────────────┘
           │
           └──────────▶ 休市 → 取消订阅 + 过滤数据 → 阻止 LLM 误判
```

---

## 📦 安装与环境配置

### 环境要求

- **Python** >= 3.10（推荐 3.11+，充分利用 asyncio 优化）
- **操作系统**：Windows / Linux / macOS 全平台兼容

### 安装依赖

```bash
pip install websockets aiohttp pytz aiofiles aiosqlite python-dotenv
```

可选依赖（仅交易模式需要）：
```bash
pip install alpaca-py      # PAPER 模式（Alpaca 模拟盘）
# OKX LIVE 模式使用纯 aiohttp + 手动签名，无需额外依赖
```

### 环境变量配置

在项目根目录创建 `.env` 文件（或直接设置系统环境变量）：

```bash
# ===== LLM 大模型配置（可选，不配置时静默跳过 LLM 分析） =====
LLM_API_KEY=sk-your-openai-api-key
LLM_API_ENDPOINT=https://api.openai.com/v1/chat/completions
LLM_MODEL=gpt-4o-mini

# ===== 交易模式（默认 OFF = 只读） =====
TRADING_MODE=OFF                            # OFF | PAPER | LIVE
DEFAULT_NOTIONAL_VALUE=1000                 # 默认名义价值（USD）

# ===== Alpaca 模拟盘（PAPER 模式） =====
ALPACA_API_KEY=your-paper-key
ALPACA_API_SECRET=your-paper-secret

# ===== OKX 实盘（LIVE 模式） =====
OKX_API_KEY=your-okx-api-key
OKX_API_SECRET=your-okx-api-secret
OKX_PASSPHRASE=your-okx-passphrase

# ===== 能量阈值配置 =====
ENERGY_THRESHOLD=5000
LLM_COOLDOWN=60
ALERT_COOLDOWN=30

# ===== 报告配置 =====
REPORT_INTERVAL=300
```

> 💡 **TRADING_MODE=OFF 时零风险！** 系统完成从行情采集到 LLM 分析的完整链路，但不发送任何订单。**先跑通 OFF 模式验证信号质量，再切换到 PAPER/LIVE。**

---

## 🚀 运行与使用

### 模式一：只读监控（OFF，默认）

零风险起步。系统完成行情采集 → 动能计算 → 能量积分 → LLM 分析 → 数据库持久化 → 5 分钟报告全链路，但不发送任何订单。

```bash
# .env
TRADING_MODE=OFF

python pipeline.py
```

---

### 模式二：Alpaca 模拟盘（PAPER）

LLM 判定的 `BUY`/`SELL` 信号自动转换为美股碎股市价单。下单前校验 `clock.is_open`，休市期间自动拦截。

**1. 配置 .env：**
```bash
TRADING_MODE=PAPER
ALPACA_API_KEY=PKxxxxxxxx     # 从 https://alpaca.markets 获取
ALPACA_API_SECRET=xxxxxxxx
DEFAULT_NOTIONAL_VALUE=1000   # 每笔下单目标名义价值（USD）
```

**2. 将 OMS 挂载到 pipeline：**

在 `pipeline.py` 的 `main()` 中，`llm_agent` 初始化之后追加：

```python
from order_manager import OrderRouter

# 在 llm_agent = MarketLLMAgent(...) 之后
router = OrderRouter(settings)
await router.initialize()
```

修改 `_llm_analyze_and_print()` 签名与内部，LLM 返回后在 `print_llm_decision(result)` 之后追加：

```python
# 在 print_llm_decision(result) 之后
if router:
    await router.process_signal(
        ticker=ticker,
        action=result["action"],
        current_price=current_price,
    )
```

**3. 启动：**
```bash
python pipeline.py
```

> ⚠️ Alpaca Paper Trading 使用模拟资金，无真实盈亏。需确保美股处于交易时段（`clock.is_open == True`），否则订单被静默拦截。

---

### 模式三：OKX 实盘（LIVE）

LLM 信号自动转换为 OKX USDT 本位永续合约市价单。系统自动拉取合约面值（`ctVal`），计算整数张数。

**1. 配置 .env：**
```bash
TRADING_MODE=LIVE
OKX_API_KEY=your-api-key          # 从 OKX 控制台创建
OKX_API_SECRET=your-api-secret
OKX_PASSPHRASE=your-passphrase
DEFAULT_NOTIONAL_VALUE=1000       # 目标名义价值（USD）
```

**2. 挂载方式同上（PAPER 模式步骤 2）。**

**3. 启动：**
```bash
python pipeline.py
```

> 🔴 **实盘风险警告：** LIVE 模式使用真实资金。建议先在 PAPER 模式验证信号质量至少一周，确认 LLM 决策可靠后再切换。OKX 合约面值在 `setup_account()` 阶段通过 `GET /api/v5/public/instruments` 自动缓存，无需手动配置。

---

### 回测模式

```bash
# 光速回测（不调用 LLM，零 API 成本）
python run_backtest.py data/sample.csv --speed 0 --mock-llm

# 10 倍速仿真回放 + 数据库持久化
python run_backtest.py data/sample.csv --speed 10 --db

# 静默模式（仅输出最终摘要）
python run_backtest.py data/sample.csv -s 5 --mock-llm -q
```

CSV 最低要求列：`timestamp`, `symbol`, `price`, `volume_usdt`。

### OMS 独立测试

```bash
python order_manager.py    # OFF 模式演示做多互斥逻辑（无需 API Key）
```

### 控制台输出说明

| 颜色 | 含义 |
|------|------|
| 🟡 **黄色高亮** | 能量异动告警（`!!! ENERGY ALERT !!!`） |
| 🟢 **绿色** | LLM 判定的多头信号（`BUY`）→ 触发开多 |
| 🔴 **红色** | LLM 判定的空头信号（`SELL`）→ 触发平多 |
| ⚪ **默认** | 中性或持有信号（`HOLD`） |
| 🔵 **蓝色** | 周期性状态报告（`STATUS REPORT`） |

### 5 分钟周期报告

系统每 5 分钟自动在 `reports/` 目录下生成结构化分析报告（文件名如 `Swap_Momentum_20260612_1430.txt`），包含涨跌幅、速度/能量统计、趋势判定、LLM 调用统计。

### 数据库查询

```python
from database import AsyncDatabaseManager
db = AsyncDatabaseManager()
await db.init_db()
ticks = await db.query_recent_ticks("TSLA-USDT-SWAP", limit=100)
decisions = await db.query_llm_decisions(ticker="TSLA-USDT-SWAP")
counts = await db.get_table_counts()
```

### 优雅停机

按下 `Ctrl+C` → 等待后台 LLM 任务完成 → 强制刷盘 → 安全退出。

---

## ⚠️ 免责声明

**本项目仅供技术研究、学术探讨与系统架构学习使用，不构成任何形式的投资建议或交易信号。**

- 加密货币及衍生品交易存在极高风险，可能导致全部本金损失。
- 本系统通过公共 WebSocket 获取行情数据，数据延迟与准确性不作保证。
- LLM 大模型输出的决策（`BUY`/`SELL`/`HOLD`）为 AI 推理结果，**不应作为真实交易的依据**——大模型存在幻觉、偏见与随机性。
- 使用者若将本系统接入真实资金交易，**需自行承担全部盈亏与法律后果**，作者不承担任何责任。
- 请遵守您所在司法管辖区的相关法律法规。某些国家/地区可能禁止或限制加密货币衍生品交易。

---

<p align="center">
  <sub>Built with Python asyncio · OKX WebSocket · aiosqlite · LLM-powered risk engine · Strategy Pattern OMS</sub>
</p>
