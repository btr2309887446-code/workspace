<p align="center">
  <h1 align="center">⚡ Synthetic Equity Swap Momentum Tracker</h1>
  <p align="center"><b>股权代币永续合约 · 动量实时监控系统</b></p>
  <p align="center">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python">
    <img alt="Async" src="https://img.shields.io/badge/Async-asyncio-orange">
    <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
    <img alt="Status" src="https://img.shields.io/badge/Status-Production%20Ready-brightgreen">
  </p>
</p>

---

## 📖 项目简介

Synthetic Equity Swap Momentum Tracker 是一套**纯 Python 异步高频量化风控系统**。它通过连接 **OKX V5 交易所公共 WebSocket**，实时监控底层锚定传统美股与韩股的 USDT 本位永续合约（如 `TSLA-USDT-SWAP`、`AAPL-USDT-SWAP`、`SAMSUNG-USDT-SWAP`）。

系统引入**物理学概念**——将价格变动抽象为**速度（Velocity）**与**能量（Energy）**，通过 EMA 平滑滤波与成交量加权，精准捕捉多空博弈中的真实资金强度。当瞬时能量突破阈值时，系统**异步唤醒 LLM 大模型**作为"风控裁判大脑"，判断异动是**真实的资金主导突破**，还是**休市期间低流动性下的假洗盘陷阱**。

> 🎯 **核心痛点解决：** 传统股市波动率指标在 24×7 加密市场中失效。加密永续合约即使底层现货休市仍有零星成交，但这些"伪动能"会误导量化策略。本系统通过**时区感知的盘口状态机**精准过滤非交易时段的垃圾信号，用 LLM 的推理能力替代硬编码规则，实现真正智能的风控裁决。

---

## ✨ 核心特性

### ⚡ 极高频纯异步架构
- 基于 `asyncio` + `websockets` + `aiohttp` 的完全异步事件循环
- 5 个独立协程并发运行（数据获取 / 盘口调度 / 消费计算 / 周期报告 / 状态打印）
- OKX WebSocket 实时推送，延迟 < 100ms，24×7 不间断运行
- LLM API 调用全程异步，**绝不阻塞主行情流**

### 🧮 物理动能微积分算法
- **Velocity (速度):** 价格一阶导数经 EMA 指数平滑 `v_t = α · (ΔP/Δt) + (1-α) · v_{t-1}`
- **Energy (能量):** 成交量加权动能 `E = |v| × Volume`，模拟物理动能公式
- **能量积分:** 梯形法则近似 `∫E dt ≈ Σ (E_i + E_{i-1}) / 2 × Δt_i`
- 环形缓冲区 (`collections.deque`)，O(1) 追加，自动淘汰历史数据，**零内存泄漏**

### 🌍 智能时区感知盘口状态机
- 基于 `pytz` 的精确时区转换（UTC ↔ KST ↔ US Eastern，自动处理夏/冬令时）
- **独立判定**：首尔时间与美东时间各自判断周末，杜绝跨时区误伤
- 覆盖 5 种盘口状态：韩股交易 / 美股盘前 / 美股盘中 / 美股盘后 / 全休市
- 休市期间**主动取消 WebSocket 订阅 + 过滤残存数据**，双重保险防伪动能

### 🧠 LLM 风控裁判大脑
- 支持 **OpenAI 兼容格式**（ChatGPT / DeepSeek / OpenClaw / Ollama / vLLM）与 **Google Gemini API** 双后端
- 超时熔断（10s Circuit Breaker）——超时直接返回 `None`，不拖累主循环
- 4 层 JSON 解析容错：剥离 Markdown → 提取花括号 → 单引号修复 → Key 加引号
- **中/英文字段双通道回退**：`action`←`动作`/`操作`/`决策`、`confidence`←`置信度`/`概率`、`reasoning`←`理由`/`逻辑`
- 每标的 60 秒冷却期，防止连续刷屏

### 🛡️ 生产级容错与优雅停机
- 所有协程 `try-except Exception` 严密包裹，单 Tick 损坏不崩溃
- 指数退避无限重连（含随机抖动 ∆±50%，防惊群效应）
- WebSocket 协议级 + OKX 应用层 `"ping"`/`"pong"` 双层心跳保活
- `asyncio.Task` 强引用集合 + `add_done_callback` 防 GC 误杀
- `SIGINT`/`SIGTERM` 信号捕获 → 安全关闭连接 → 有序退出

---

## 🏗 系统架构与模块说明

```
swap momentum tracker/
├── config.py              # 配置模块（标的池、API端点、阈值、日志）
├── session_manager.py     # 时区与盘口状态机（pytz 时区转换 + 周末判定）
├── data_fetcher.py        # OKX WebSocket 数据获取（订阅/重连/心跳）
├── analytics.py           # 核心算法（Velocity/Energy/能量积分/跳空检测）
├── llm_agent.py           # LLM 风控裁判（多后端兼容/超时熔断/JSON解析）
├── reporter.py            # 报告与持久化（告警打印 + 5分钟.txt报告）
├── pipeline.py            # 主调度引擎（asyncio 事件循环 + 协程编排）
├── reports/               # 周期报告输出目录
├── logs/                  # 轮转日志目录
└── .env                   # 环境变量配置（可选）
```

### 模块职责与数据流

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `config.py` | 集中管理所有可调参数、API 密钥、能量阈值、日志配置 | 环境变量 / `.env` | `Settings` 实例 |
| `session_manager.py` | 判定当前 KST/ET 时间所处盘口，返回活跃标的列表 | UTC 时间 | `SessionInfo`（active/swaps/suppressed） |
| `data_fetcher.py` | 连接 OKX WebSocket，订阅 Ticker 频道，解析 JSON | 活跃标的列表 | `asyncio.Queue`（标准化行情 dict） |
| `analytics.py` | 维护每个标的的环形缓冲区，计算速度/能量/跳空检测 | 行情数据 | 更新后的指标快照 + 窗口聚合统计 |
| `llm_agent.py` | 异步调用 LLM API，解析 JSON 决策 | 量价特征 + 5分钟统计 | `{action, confidence, reasoning}` |
| `reporter.py` | 格式化告警输出、生成 5 分钟 `.txt` 报告 | 计算器 + 数据获取器状态 | 控制台打印 + 文件写入 |
| `pipeline.py` | 协调 5 个并发协程的生命周期，事件驱动 LLM 触发 | 配置 | 系统运行 |

```
                          ┌─────────────────────┐
                          │     config.py        │ ◀── 环境变量 / .env
                          └────────┬────────────┘
                                   │ Settings 注入
    ┌──────────────┐  ┌────────────┴────────────┐  ┌──────────────────┐
    │SessionManager│  │     pipeline.py          │  │   LLMAgent       │
    │              │  │  ┌───────────────────┐   │  │                  │
    │ 韩股/美股盘口 │  │  │ SessionPoller     │   │  │ OpenAI/Gemini    │
    │ 周末判定     │  │  │ update_symbols()  │   │  │ 超时熔断         │
    │ 标的切换    │──│▶│▶ DataFetcher       │   │  │ JSON 中英回退    │
    └──────────────┘  │  └────────┬──────────┘   │  └────────▲─────────┘
                      │           │ Queue         │           │
                      │  ┌────────▼──────────┐   │  ┌────────┴─────────┐
                      │  │ Consumer          │   │  │ Energy Alert     │
                      │  │ ├─ Filter by ses. │   │  │ > threshold?     │
                      │  │ ├─ Calc.update()  │───│──│ create_task(llm) │
                      │  │ ├─ Alert check    │   │  └──────────────────┘
                      │  │ └─ Print tick     │   │
                      │  └───────────────────┘   │  ┌──────────────────┐
                      │  ┌───────────────────┐   │  │ PeriodicReporter │
                      │  │ StatsPrinter      │   │  │ 5-min .txt report│
                      │  └───────────────────┘   │  └──────────────────┘
                      └──────────────────────────┘
```

---

## 📦 安装与环境配置

### 环境要求

- **Python** >= 3.10（推荐 3.11+，充分利用 asyncio 优化）
- **操作系统**：Windows / Linux / macOS 全平台兼容

### 安装依赖

```bash
pip install websockets aiohttp pytz aiofiles python-dotenv
```

可选依赖（若需接入特定 API）：
```bash
pip install alpaca-py          # Alpaca Markets 数据源（非必需）
pip install yfinance           # Yahoo Finance 数据源（非必需）
```

### 环境变量配置

在项目根目录创建 `.env` 文件（或直接设置系统环境变量）：

```bash
# ===== LLM 大模型配置 =====
LLM_API_KEY=sk-your-openai-api-key          # OpenAI/DeepSeek API Key
LLM_API_ENDPOINT=https://api.openai.com/v1/chat/completions
# 或 Gemini: https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent
LLM_MODEL=gpt-4o-mini                       # 模型名称

# ===== 能量阈值配置 =====
ENERGY_THRESHOLD=5000                       # 触发 LLM 的能量阈值（默认 5000）
LLM_COOLDOWN=60                             # LLM 冷却期（秒，默认 60）
ALERT_COOLDOWN=30                           # 告警冷却期（秒，默认 30）

# ===== 报告配置 =====
REPORT_INTERVAL=300                         # 周期报告间隔（秒，默认 300 = 5分钟）
```

> 💡 **无 LLM 配置也可运行！** 系统会在未检测到 LLM API Key 时静默跳过 LLM 分析，仅输出能量告警和终端指标。量化计算与报告生成功能完全独立于 LLM。

---

## 🚀 运行与使用

### 启动命令

```bash
python pipeline.py
```

启动后将看到启动横幅与控制台实时数据流：

```
+==============================================================================+
|      SYNTHETIC EQUITY SWAP MOMENTUM TRACKER v1.0                         |
|        股权代币永续合约动量实时监控系统                                      |
+------------------------------------------------------------------------------+
|  KRX Swaps  : SAMSUNG-USDT-SWAP, SKHYNIX-USDT-SWAP                          |
|  US Swaps   : TSLA-USDT-SWAP, NVDA-USDT-SWAP, AAPL-USDT-SWAP, ...           |
|  LLM Engine : Configured (gpt-4o-mini)                                       |
|  Energy Thr : 5,000                                                          |
+==============================================================================+

──────────────────────────────────────────────────────────────────────────────
  Contract                Price       Velocity         Energy
──────────────────────────────────────────────────────────────────────────────
  TSLA:248.5000 +12.3456 | NVDA:980.2000 -3.4567 | AAPL:195.6789 +0.2345
```

### 控制台输出说明

| 颜色 | 含义 |
|------|------|
| 🟡 **黄色高亮** | 能量异动告警（`!!! ENERGY ALERT !!!`） |
| 🟢 **绿色** | LLM 判定的多头信号（`BUY`） |
| 🔴 **红色** | LLM 判定的空头信号（`SELL`） |
| ⚪ **默认** | 中性或持有信号（`HOLD`） |
| 🔵 **蓝色** | 周期性状态报告（`STATUS REPORT`） |

### 实时数据行解读

```
TSLA:248.5000 +12.3456  →  特斯拉合约当前价 248.5000 USDT，速度 +12.3456 USDT/s（上涨方向）
NVDA:980.2000 -3.4567   →  英伟达合约当前价 980.2000 USDT，速度 -3.4567 USDT/s（下跌方向）
```

### 5 分钟周期报告

系统每 5 分钟自动在 `reports/` 目录下生成一份结构化分析报告（文件名如 `Swap_Momentum_20260612_1430.txt`），包含：

- 每只活跃标的的**涨跌幅**与**价格区间**
- 速度指标：平均速度、标准差、最大/最小瞬时速度及对应价格
- 能量指标：能量积分 (∫E dt)、平均能量、最高/最低能量极值点
- 趋势判定：**买盘主导 / 卖盘主导 / 多空均衡** + 买卖力量占比
- LLM 调用统计：总调用次数、成功率、超时次数

### 优雅停机

按下 `Ctrl+C` → 系统捕获 `SIGINT` → 关闭 WebSocket 连接 → 等待协程退出 → 安全退出。

```
收到停机信号，开始有序关闭...
数据获取器已停止
所有任务已安全退出
程序已安全关闭。
```

---

## ⚠️ 免责声明

**本项目仅供技术研究、学术探讨与系统架构学习使用，不构成任何形式的投资建议或交易信号。**

- 加密货币及衍生品交易存在极高风险，可能导致全部本金损失。
- 本系统通过公共 WebSocket 获取行情数据，数据延迟与准确性不作保证。
- LLM 大模型输出的决策（`BUY`/`SELL`/`HOLD`）为 AI 推理结果，**不应作为真实交易的依据**——大模型存在幻觉、偏见与随机性，其判断在金融场景下可能完全错误。
- 使用者若将本系统接入真实资金交易，**需自行承担全部盈亏与法律后果**，作者不承担任何责任。
- 请遵守您所在司法管辖区的相关法律法规。某些国家/地区可能禁止或限制加密货币衍生品交易。

---

<p align="center">
  <sub>Built with ❤️ by a quant dev · Python asyncio · OKX WebSocket · LLM-powered risk engine</sub>
</p>
