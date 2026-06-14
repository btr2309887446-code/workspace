<p align="center">
  <h1 align="center">⚡ Synthetic Equity Swap Momentum Tracker</h1>
  <p align="center"><b>股权代币永续合约 · 高频动量监控 · LLM风控 · 全自动交易</b></p>
  <p align="center">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python">
    <img alt="Async" src="https://img.shields.io/badge/Async-asyncio-orange">
    <img alt="Lines" src="https://img.shields.io/badge/Lines-~7500-9cf">
    <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
    <img alt="SQLite" src="https://img.shields.io/badge/DB-SQLite%20(aiosqlite)-lightgrey">
  </p>
</p>

---

## 📖 项目简介

Synthetic Equity Swap Momentum Tracker 是一套**纯 Python 异步高频量化风控与自动交易系统**（~7500 行，14 模块）。通过 **OKX V5 WebSocket** 实时监控底层锚定美股与韩股的 USDT 本位永续合约（`TSLA-USDT-SWAP`、`AAPL-USDT-SWAP`、`SAMSUNG-USDT-SWAP` 等）。

系统引入**物理学概念**——价格变动抽象为 **Velocity（速度）** 与 **Energy（能量）**，经 EMA 平滑 + 成交量加权，配合 **实时特征工程引擎（15+ 维 Z-Score/OBI/RSI/VWAP）**。当能量突破阈值时**异步唤醒 LLM** 进行多维度风控裁决，通过**策略模式 OMS** 自动执行订单。

> 🎯 **核心闭环**：OKX 实时行情 → 15维特征引擎 → LLM 风控裁判 → OMS 自动下单 → 4表时序数据库 → 5分钟报告 → 新闻情绪注入

---

## ✨ 核心特性

### ⚡ 极高频纯异步架构
- `asyncio` + `websockets` + `aiohttp` 全异步事件循环，5 协程并发
- OKX WebSocket 延迟 < 100ms，24×7 不间断
- I/O 全程异步：LLM API、数据库批量刷写、订单执行

### 🧮 实时特征工程引擎
- **基础层**：Velocity / Energy / Raw Speed / Gap Detection
- **订单簿**：Spread / Spread% / OBI (Order Book Imbalance) / VWAP
- **标准化层**：`_RollingStats` O(1) 增量 Z-Score（双累加器 + deque 滑动窗口）
- **技术指标**：Tick级 RSI（Wilder's Smoothing）/ 微观波动率
- 全部 O(1) 时间复杂度，零 pandas 依赖，`deque(maxlen)` 零泄漏

### 🌍 时区感知盘口状态机
- `pytz` 精确转换 UTC↔KST↔US Eastern，自动夏/冬令时
- 5 种状态独立周末判定，杜绝跨时区误休
- 休市期间取消订阅 + 过滤残存数据，双重防伪动能

### 🧠 LLM 风控裁判
- OpenAI / DeepSeek / Gemini 双后端自动路由
- 5 层 JSON 解析 + 中英文键双通道回退
- Prompt 含 Z-Score/OBI/RSI/VWAP/微观波动率等 15+ 维语义字段
- 10s 超时熔断 + 每标的 60s 冷却期

### 📊 OMS 自动交易
- `BaseExecutor` 策略模式 → `AlpacaPaperExecutor` / `OKXLiveExecutor`
- 名义价值驱动 + 做多互斥 + HMAC SHA256 签名
- `TRADING_MODE=OFF/PAPER/LIVE` 三级控制

### 📰 新闻情绪引擎
- RSS 轮询 + MacroSentimentPool 时间衰减池
- LLM 打分为 `impact_score`（-1~1），注入 analytics 快照
- 回测支持双轨时间线合并（Tick + News 交织回放）

### 💾 4 表时序数据库
- `ticks_history` / `llm_decisions` / `energy_alerts` / `order_executions`
- `aiosqlite` 异步 SQLite + 500 条批量刷写 + 停机强制刷盘

### 🔄 完整回测沙盒
- 双轨 Feeder：Tick + News Parquet 按时间戳归并回放
- `BacktestBroker`：虚拟撮合 + 0.05% 摩擦成本 + 夏普/最大回撤/胜率
- 历史数据工具链：`okx_data_crawler` 下载 + `historical_news_pipeline` 新闻预处理

### 🛡️ 生产级容错
- 全部 try-except 严密包裹，指数退避无限重连
- Task 强引用集合防 GC 误杀，停机等待后台任务 → 刷盘 → 退出

---

## 🏗 系统架构

```
swap momentum tracker/                    (~7500 lines / 14 modules)
├── config.py                 # 配置中心（标的池/API密钥/阈值/交易模式）
├── session_manager.py        # 时区状态机（KST/ET 独立周末判定）
├── data_fetcher.py           # OKX WebSocket（订阅/重连/双层心跳）
├── analytics.py              # ★ 实时特征引擎（15维 O(1)计算）
├── llm_agent.py              # LLM 风控裁判（OpenAI+Gemini/中英回退）
├── order_manager.py          # OMS 策略模式（Alpaca/OKX/做多互斥）
├── news_engine.py            # ★ 新闻情绪引擎（RSS+LLM+时间衰减池）
├── reporter.py               # 彩色告警 + 5分钟 .txt 报告
├── database.py               # 时序数据库（4表/500批量/aiosqlite）
├── backtest_feeder.py        # ★ 双轨回放器（Tick+News 时间线归并）
├── backtest_broker.py        # ★ 虚拟撮合引擎（摩擦成本+绩效报告）
├── okx_data_crawler.py       # ★ OKX CDN 历史数据下载（parquet输出）
├── historical_news_pipeline.py # ★ 新闻预处理管道（ckpt+LLM打分）
├── run_backtest.py           # 沙盒启动器（依赖注入/跳过盘口过滤）
├── api_server.py             # ★ FastAPI 云端 API（REST + WebSocket）
├── dashboard.py              # ★ Streamlit 可视化面板
├── launcher.py               # ★ 统一启动器（panel / live / backtest）
├── pipeline.py               # 主调度引擎（5协程并发）
├── reports/                  # 周期报告
├── logs/                     # 轮转日志
├── data/historical/          # 历史数据（parquet）
└── .env                      # 环境变量
```

### 数据流全景

```
                    ┌──────────────┐
                    │  Config/.env │
                    └──────┬───────┘
                           │
   ┌───────────────────────┼───────────────────────────┐
   │                       │                           │
   ▼                       ▼                           ▼
┌──────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ Session  │    │    Pipeline      │    │  Historical Tools    │
│ Manager  │    │                  │    │                      │
│ 时区/盘口│    │ DataFetcher ─┐   │    │ okx_data_crawler     │
│ 周末判定│    │ (OKX WS)     │   │    │ → .parquet           │
└────┬─────┘    │       │      │   │    │                      │
     │          │       ▼      │   │    │ historical_news_     │
     │          │   Queue      │   │    │ pipeline             │
     │          │       │      │   │    │ → news_sentiment     │
     │          │       ▼      │   │    │   .parquet           │
     │          │  Consumer ─┐ │   │    └──────────┬───────────┘
     │          │  • 盘口过滤 │ │   │               │
     │          │  • 15维特征 │ │   │               ▼
     │          │  • 能量阈值 │ │   │    ┌──────────────────────┐
     │          │       │    │ │   │    │  Run Backtest        │
     │          │       ▼    │ │   │    │  • Dual Feeder       │
     │          │  ┌─────────┐│ │   │    │  • BacktestBroker   │
     │          │  │Database ││ │   │    │  • Mock LLM         │
     │          │  │4表 500条││ │   │    │  • 绩效报告          │
     │          │  └─────────┘│ │   │    └──────────────────────┘
     │          │       │    │ │   │
     │          │       ▼    │ │   │
     │          │  LLM Agent │ │   │    ┌──────────────────────┐
     │          │  • 风控裁决 │ │   │    │  News Engine         │
     │          │       │    │ │   │    │  • RSS 轮询           │
     │          │       ▼    │ │   │    │  • LLM 情绪评分       │
     │          │  OrderRouter│ │   │    │  • 时间衰减池         │
     │          │  • 做多互斥 │◄┼───┼────│  • get_current_bias() │
     │          │  • Alpaca   │ │   │    └──────────────────────┘
     │          │  • OKX      │ │   │
     │          └─────────────┘ │   │
     └──────────────────────────┘   │
                                    │
                           ┌────────┴────────┐
                           │   Reporter      │
                           │ • 控制台告警     │
                           │ • 5分钟报告     │
                           └─────────────────┘
```

---

## 📦 环境要求

- **Python** >= 3.10 · Windows / Linux / macOS

### 一键安装

```bash
# 核心依赖（命令行模式必装）
pip install websockets aiohttp pytz aiofiles aiosqlite python-dotenv pandas pyarrow tqdm

# 可视化面板（模式 A 必装）
pip install fastapi uvicorn streamlit plotly nest-asyncio

# 可选
pip install alpaca-py    # Alpaca 模拟盘 / 历史新闻 API
```

### 环境变量

创建 `.env` 文件：

```bash
# LLM（必填，否则跳过 LLM 分析仅输出指标）
LLM_API_KEY=sk-your-key
LLM_API_ENDPOINT=https://api.deepseek.com/v1/chat/completions
LLM_MODEL=deepseek-chat

# 交易模式（默认 OFF 只读）
TRADING_MODE=OFF                          # OFF | PAPER | LIVE
DEFAULT_NOTIONAL_VALUE=1000

# Alpaca 模拟盘
ALPACA_API_KEY=PKxxx
ALPACA_API_SECRET=xxx

# OKX 实盘
OKX_API_KEY=xxx
OKX_API_SECRET=xxx
OKX_PASSPHRASE=xxx

# 阈值
ENERGY_THRESHOLD=5000
LLM_COOLDOWN=60
ALERT_COOLDOWN=30
REPORT_INTERVAL=300
```

---

## 🚀 启动指南（推荐按顺序执行）

### 第一步：安装依赖

```bash
pip install websockets aiohttp pytz aiofiles aiosqlite python-dotenv \
            pandas pyarrow tqdm fastapi uvicorn streamlit plotly \
            nest-asyncio websockets
```

可选：
```bash
pip install alpaca-py    # Alpaca 模拟盘 / 历史新闻
```

### 第二步：配置 .env

在项目根目录创建 `.env` 文件（已有则跳过）：

```bash
# ===== 必填：LLM 配置（不填则跳过 LLM 分析） =====
LLM_API_KEY=sk-your-api-key
LLM_API_ENDPOINT=https://api.deepseek.com/v1/chat/completions
LLM_MODEL=deepseek-chat

# ===== 交易模式 =====
TRADING_MODE=OFF              # OFF（只读/默认）| PAPER | LIVE
DEFAULT_NOTIONAL_VALUE=1000

# ===== 阈值 =====
ENERGY_THRESHOLD=5000
LLM_COOLDOWN=60
ALERT_COOLDOWN=30
```

> 💡 **LLM_API_KEY 不填也能跑！** 系统会自动跳过 LLM 分析环节，仅输出动能指标和控制台告警。

### 第三步：选择运行模式（统一入口 `launcher.py`）

所有模式均通过 **`python launcher.py <mode>`** 启动，无需记忆多个脚本名：

```bash
python launcher.py panel              # 模式 A：可视化面板
python launcher.py live               # 模式 B：命令行实时监控
python launcher.py backtest <file>    # 模式 C：历史回测
```

---

#### 🖥️ 模式 A：可视化面板

```bash
python launcher.py panel
```

自动启动 FastAPI 后端（`http://localhost:8000`）和 Streamlit 面板（`http://localhost:8501`），`Ctrl+C` 同时关闭两个服务。

> 也支持传统两步启动：`uvicorn api_server:app --port 8000` + `streamlit run dashboard.py`，效果相同。

---

#### ⌨️ 模式 B：命令行实时监控

```bash
python launcher.py live
```

所有输出打印到终端。数据库和报告自动写入 `data/` 和 `reports/`。等同于 `python pipeline.py`。

---

#### 📊 模式 C：历史回测

```bash
# 光速回测（不调 LLM，零成本）
python launcher.py backtest data/historical/TSLA-USDT-SWAP/2026-01-15.parquet --speed 0 --mock-llm

# 带虚拟交易 + 数据库持久化
python launcher.py backtest data/ticks.csv -s 10 --mock-llm --trade --db

# 静默模式（仅输出绩效报告）
python launcher.py backtest data/ticks.parquet -s 0 --mock-llm --trade -q
```

回测结束后自动输出绩效报告（净值、最大回撤、夏普、胜率）。等同于 `python run_backtest.py`。

---

#### 🧪 独立模块测试

```bash
python launcher.py live               # 完整引擎（与模式 B 相同）
python news_engine.py                 # 新闻情绪 Demo（无需 API）
python order_manager.py               # OMS 做多互斥 Demo
```

---

### 完整工作流推荐

```
1. 配置 .env ──→ 2. python launcher.py live 跑 10 分钟验证信号 ──→ 3. Ctrl+C
                      │
                      ▼ 信号质量满意？
               ┌──────┴──────┐
               ▼              ▼
    回测验证策略          直接开启交易
    python okx_data_crawler.py    TRADING_MODE=PAPER
    python launcher.py backtest   python launcher.py live
    (查看绩效报告)                (Alpaca 模拟盘)
               │                      │
               ▼                      ▼
         满意后开启交易          实盘前先用 PAPER
         TRADING_MODE=PAPER      验证至少一周
         python launcher.py live
```

### 交易模式切换

| 模式 | `.env` 设置 | 命令 | 说明 |
|------|-----------|------|------|
| 可视化面板 | 任意 | `python launcher.py panel` | 一键启动 API+面板 |
| 只读监控 | `TRADING_MODE=OFF` | `python launcher.py live` | 终端运行，不下单 |
| 模拟盘 | `TRADING_MODE=PAPER` + Alpaca | `python launcher.py live` | 美股碎股模拟 |
| 实盘 | `TRADING_MODE=LIVE` + OKX | `python launcher.py live` | OKX 合约实盘 |

---

## 📊 控制台输出

| 颜色 | 含义 |
|------|------|
| 🟡 **黄色** | 能量突破告警 |
| 🟢 **绿色** | LLM 判定 BUY |
| 🔴 **红色** | LLM 判定 SELL |
| 🔵 **蓝色** | 周期性状态报告 |

---

## ⚠️ 免责声明

**本项目仅供技术研究和架构学习，不构成投资建议。**

加密货币及衍生品交易存在极高风险。LLM 决策为 AI 推理结果，存在幻觉与随机性。使用者若接入真实资金交易，需自行承担全部盈亏与法律后果。

---

<p align="center">
  <sub>Built with Python asyncio · OKX WebSocket · 15-dim Feature Engine · LLM Risk Engine · OMS · News Sentiment · Dual-Stream Backtest</sub>
</p>
