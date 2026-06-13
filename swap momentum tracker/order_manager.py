"""
订单管理系统 (order_manager.py)
================================
职责：
  1. 接收 LLM 风控判决 (BUY/SELL/HOLD) 与动量指标，执行实际下单操作
  2. 策略模式 (Strategy Pattern)：通过 BaseExecutor 抽象基类定义统一接口
  3. 支持 Alpaca 模拟盘（美股碎股）与 OKX 实盘（USDT 永续合约）无缝切换
  4. OrderRouter 实现单向做多互斥逻辑 (Long-Only Mutex)，
     防止同向无限加仓

架构：
  OrderRouter
    ├── AlpacaPaperExecutor  (trading_mode=PAPER)
    └── OKXLiveExecutor      (trading_mode=LIVE)

名义价值驱动 (Notional Value Driven)：
  - 用户指定目标名义价值（如 $1000 USD）
  - Alpaca: qty = notional_value / price（碎股）
  - OKX:    sz  = int(notional_value / (price × contract_face_value))
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional, Any

import aiohttp

from config import Settings, Ansi

logger = logging.getLogger("SwapMomentum.OrderManager")


# ============================================================================
# 抽象执行器基类
# ============================================================================

class BaseExecutor(ABC):
    """
    订单执行器抽象基类。

    所有具体执行器（Alpaca/OKX）必须实现以下接口：
      - setup_account(): 初始化账户、拉取并缓存交易规则
      - execute_order():  执行下单
    """

    @abstractmethod
    async def setup_account(self) -> None:
        """
        初始化账户信息。
        包括：获取资金余额、当前持仓、拉取并缓存交易规则
        （如合约面值、最小交易单位等）。
        """
        ...

    @abstractmethod
    async def execute_order(
        self,
        symbol: str,
        action: str,
        notional_value: float,
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        执行下单。

        Args:
            symbol:         标的代码（如 TSLA-USDT-SWAP）
            action:         BUY / SELL
            notional_value: 目标名义价值（USD）
            current_price:  当前参考价格

        Returns:
            订单响应 dict，失败返回 None
        """
        ...


# ============================================================================
# Alpaca 模拟盘执行器
# ============================================================================

class AlpacaPaperExecutor(BaseExecutor):
    """
    Alpaca Markets 模拟盘执行器。

    特性：
      - 自动映射合约代码 → 美股现货代码（TSLA-USDT-SWAP → TSLA）
      - 支持 Fractional Shares（碎股）市价单
      - 下单前校验美股盘口状态（clock.is_open），休市期间拒绝发送
    """

    # 合约代码 → 美股现货代码映射表
    SYMBOL_MAP: Dict[str, str] = {
        "TSLA-USDT-SWAP": "TSLA",
        "NVDA-USDT-SWAP": "NVDA",
        "AAPL-USDT-SWAP": "AAPL",
        "MU-USDT-SWAP":   "MU",
        "WDC-USDT-SWAP":  "WDC",
        "MRVL-USDT-SWAP": "MRVL",
    }

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._trading_client = None
        self._clock = None

        # 下单统计
        self.stats = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "market_closed_blocks": 0,
        }

    async def setup_account(self) -> None:
        """
        初始化 Alpaca 客户端并验证连接。

        包括：
          1. 创建 TradingClient 实例
          2. 获取当前市场时钟（用于休市拦截）
        """
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetClockRequest

            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=True,  # 强制模拟盘
            )

            # 异步线程池获取时钟
            clock = await asyncio.to_thread(
                lambda: self._trading_client.get_clock(GetClockRequest())
            )
            self._clock = clock

            logger.info(
                f"Alpaca 模拟盘已连接 | is_open={clock.is_open} | "
                f"next_open={clock.next_open} | next_close={clock.next_close}"
            )

        except ImportError:
            logger.error("alpaca-py 未安装，无法使用 Alpaca 执行器")
            raise
        except Exception:
            logger.exception("Alpaca 初始化失败")
            raise

    async def execute_order(
        self,
        symbol: str,
        action: str,
        notional_value: float,
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        在 Alpaca 模拟盘中执行下单。

        流程：
          1. 映射合约代码 → 美股现货代码
          2. 校验盘口状态（clock.is_open）
          3. 计算碎股数量 qty = notional_value / current_price
          4. 提交市价单
        """
        # 符号映射
        stock_symbol = self._map_symbol(symbol)
        if stock_symbol is None:
            logger.error(f"Alpaca: 无法映射 {symbol} → 美股代码，拒绝下单")
            self.stats["orders_rejected"] += 1
            return None

        # 盘口校验
        if self._clock is None or not self._clock.is_open:
            logger.warning(
                f"Alpaca: 美股休市，拒绝 {action} {stock_symbol} "
                f"@ {current_price:.2f}"
            )
            self.stats["market_closed_blocks"] += 1
            return None

        # 碎股计算
        qty = notional_value / current_price

        logger.info(
            f"Alpaca 下单 | {action} {stock_symbol} | "
            f"notional=${notional_value:,.0f} | qty={qty:.6f} shares"
        )

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            side = OrderSide.BUY if action.upper() == "BUY" else OrderSide.SELL

            order_req = MarketOrderRequest(
                symbol=stock_symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )

            # 异步线程池提交
            order = await asyncio.to_thread(
                lambda: self._trading_client.submit_order(order_req)
            )

            self.stats["orders_submitted"] += 1
            self.stats["orders_filled"] += 1

            logger.info(
                f"Alpaca 订单成交 | id={order.id} | "
                f"{order.side} {order.symbol} qty={order.qty} "
                f"status={order.status}"
            )

            return {
                "broker": "alpaca",
                "order_id": str(order.id),
                "symbol": stock_symbol,
                "action": action,
                "qty": qty,
                "status": str(order.status),
            }

        except ImportError:
            logger.error("alpaca-py 未安装")
            self.stats["orders_rejected"] += 1
        except Exception:
            logger.exception(f"Alpaca 下单失败 | {action} {stock_symbol}")
            self.stats["orders_rejected"] += 1

        return None

    def _map_symbol(self, symbol: str) -> Optional[str]:
        """
        将合约代码映射为美股现货代码。

        TSLA-USDT-SWAP → TSLA
        若不在映射表中，尝试直接截取第一个 '-' 之前的部分。
        """
        if symbol in self.SYMBOL_MAP:
            return self.SYMBOL_MAP[symbol]
        # 兜底：截取 TSLA-USDT-SWAP → TSLA
        parts = symbol.split("-")
        if parts:
            return parts[0]
        return None


# ============================================================================
# OKX 实盘执行器
# ============================================================================

class OKXLiveExecutor(BaseExecutor):
    """
    OKX V5 实盘执行器（USDT 本位永续合约）。

    特性：
      - 动态缓存合约面值 (ctVal)，避免每次下单都请求 API
      - 手动实现 HMAC SHA256 签名算法
      - 张数精准换算：sz = int(notional_value / (price × face_value))
      - 纯异步 aiohttp，不阻塞主事件循环
    """

    OKX_REST_URL = "https://www.okx.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

        # 合约面值缓存：{ "TSLA-USDT-SWAP": 1.0, ... }
        self.multipliers: Dict[str, float] = {}

        # 下单统计
        self.stats = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "multiplier_cache_hits": 0,
            "multiplier_cache_misses": 0,
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def setup_account(self) -> None:
        """
        初始化 OKX 账户。

        1. 拉取所有 SWAP 合约的公共信息
        2. 提取 instId → ctVal 映射，缓存到 self.multipliers
        3. 可选：校验 API Key 有效性
        """
        logger.info("OKX 执行器初始化中...")

        # 拉取合约面值
        await self._fetch_instruments()

        logger.info(
            f"OKX 执行器就绪 | 缓存合约数={len(self.multipliers)}"
        )

    async def execute_order(
        self,
        symbol: str,
        action: str,
        notional_value: float,
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        在 OKX 实盘中执行下单。

        流程：
          1. 从缓存获取合约面值
          2. 计算合约张数 sz = int(notional / (price × face_value))
          3. 构建签名并发送 POST /api/v5/trade/order
        """
        # 获取合约面值
        face_value = self.multipliers.get(symbol)
        if face_value is None:
            logger.error(
                f"OKX: 合约 {symbol} 面值未缓存，拒绝下单。"
                f"请确认 setup_account() 已成功执行"
            )
            self.stats["orders_rejected"] += 1
            self.stats["multiplier_cache_misses"] += 1
            return None

        self.stats["multiplier_cache_hits"] += 1

        # 计算张数（向下取整，OKX 要求整数张）
        notional_per_contract = current_price * face_value
        if notional_per_contract <= 0:
            logger.error(f"OKX: {symbol} 合约面值或价格异常，拒绝下单")
            self.stats["orders_rejected"] += 1
            return None

        sz = int(notional_value / notional_per_contract)
        if sz <= 0:
            logger.warning(
                f"OKX: {symbol} 名义价值 ${notional_value:,.0f} 不足以开 1 张合约 "
                f"(每张=${notional_per_contract:,.2f})，忽略"
            )
            return None

        # OKX 下单方向映射
        if action.upper() == "BUY":
            side = "buy"
            pos_side = "long"
        else:
            side = "sell"
            pos_side = "long"  # 平多

        body = {
            "instId": symbol,
            "tdMode": "cross",     # 全仓保证金
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(sz),
        }

        logger.info(
            f"OKX 下单 | {action} {symbol} | "
            f"notional=${notional_value:,.0f} | "
            f"face_value={face_value} | sz={sz}张"
        )

        # 签名并发送
        try:
            result = await self._signed_request(
                method="POST",
                path="/api/v5/trade/order",
                body=body,
            )

            if result is None:
                self.stats["orders_rejected"] += 1
                return None

            code = result.get("code", "")
            if code == "0":
                data = result.get("data", [{}])[0]
                self.stats["orders_submitted"] += 1
                self.stats["orders_filled"] += 1

                logger.info(
                    f"OKX 订单成交 | ordId={data.get('ordId')} | "
                    f"{side} {sz}张 {symbol} | sCode={data.get('sCode')}"
                )

                return {
                    "broker": "okx",
                    "order_id": data.get("ordId", ""),
                    "symbol": symbol,
                    "action": action,
                    "sz": sz,
                    "face_value": face_value,
                    "status": "filled",
                }
            else:
                logger.error(
                    f"OKX 订单失败 | code={code} | "
                    f"msg={result.get('msg', '')} | data={result.get('data', [])}"
                )
                self.stats["orders_rejected"] += 1

                # 特殊错误码：余额不足
                if code in ("51008", "51009", "51121"):
                    logger.error(
                        f"OKX 资金不足！无法完成 {action} {sz}张 {symbol}"
                    )

        except asyncio.TimeoutError:
            logger.error(f"OKX 下单超时 | {action} {symbol}")
            self.stats["orders_rejected"] += 1
        except aiohttp.ClientError as e:
            logger.error(f"OKX 网络异常: {e}")
            self.stats["orders_rejected"] += 1
        except Exception:
            logger.exception(f"OKX 下单未预期异常 | {action} {symbol}")
            self.stats["orders_rejected"] += 1

        return None

    # ------------------------------------------------------------------
    # 内部：合约面值缓存
    # ------------------------------------------------------------------

    async def _fetch_instruments(self) -> None:
        """
        拉取所有 SWAP 合约的公共信息，缓存 instId → ctVal 映射。

        GET /api/v5/public/instruments?instType=SWAP
        """
        try:
            data = await self._signed_request(
                method="GET",
                path="/api/v5/public/instruments",
                params={"instType": "SWAP"},
                auth_required=False,  # 公共接口无需签名
            )

            if data and data.get("code") == "0":
                instruments = data.get("data", [])
                for inst in instruments:
                    inst_id = inst.get("instId", "")
                    ct_val = inst.get("ctVal", "")
                    if inst_id and ct_val:
                        try:
                            self.multipliers[inst_id] = float(ct_val)
                        except (ValueError, TypeError):
                            logger.warning(
                                f"OKX: {inst_id} ctVal 解析失败: {ct_val}"
                            )

                logger.info(
                    f"OKX 合约面值缓存完成 | total={len(self.multipliers)}"
                )
            else:
                logger.error(
                    f"OKX 获取合约信息失败 | {data}"
                )

        except Exception:
            logger.exception("OKX 拉取合约面值异常")

    # ------------------------------------------------------------------
    # 内部：签名与 HTTP 请求
    # ------------------------------------------------------------------

    async def _signed_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        auth_required: bool = True,
    ) -> Optional[dict]:
        """
        发送 OKX V5 签名 HTTP 请求。

        签名算法 (HMAC SHA256)：
          timestamp = ISO 8601 UTC
          sign_msg  = timestamp + method + path + body_str
          signature = base64(HMAC-SHA256(sign_msg, secret_key))

        Args:
            method:        GET / POST
            path:          API 路径（如 /api/v5/trade/order）
            body:          请求体 dict（POST 时）
            params:        URL 查询参数
            auth_required: 是否需要签名鉴权

        Returns:
            响应 JSON dict，失败返回 None
        """
        url = f"{self.OKX_REST_URL}{path}"
        body_str = json.dumps(body) if body else ""

        headers = {"Content-Type": "application/json"}

        if auth_required:
            timestamp = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )
            sign_msg = timestamp + method.upper() + path + body_str
            signature = base64.b64encode(
                hmac.new(
                    self.api_secret.encode("utf-8"),
                    sign_msg.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
            ).decode("utf-8")

            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-SIGN"] = signature
            headers["OK-ACCESS-TIMESTAMP"] = timestamp
            headers["OK-ACCESS-PASSPHRASE"] = self.passphrase
            # 模拟盘模式
            headers["x-simulated-trading"] = "1"

        try:
            async with aiohttp.ClientSession() as session:
                if method.upper() == "GET":
                    async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        return await resp.json()
                else:
                    async with session.post(
                        url,
                        json=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        return await resp.json()

        except asyncio.TimeoutError:
            logger.error(f"OKX API 超时 | {method} {path}")
        except aiohttp.ClientError as e:
            logger.error(f"OKX API 网络异常: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"OKX API 响应非 JSON: {e}")
        except Exception:
            logger.exception(f"OKX API 未预期异常 | {method} {path}")

        return None


# ============================================================================
# 统一路由调度中心 (OrderRouter)
# ============================================================================

class OrderRouter:
    """
    订单路由调度中心。

    职责：
      1. 根据 config.trading_mode 动态实例化对应的执行器
      2. 实现单向做多互斥逻辑 (Long-Only Mutex)
      3. 维护内存持仓状态 self.positions
      4. 仅处理 LLM 判定的 BUY / SELL 信号，HOLD 直接忽略

    做多互斥规则：
      BUY   + 无持仓 → 开多
      BUY   + 已有多 → 忽略（防止无限加仓）
      SELL  + 已有多 → 平多
      SELL  + 无持仓 → 忽略
    """

    def __init__(self, settings: Settings):
        """
        初始化订单路由器。

        Args:
            settings: Settings 配置实例
        """
        self.settings = settings
        self.mode = settings.trading_mode.upper()

        # 持仓状态：{ "TSLA-USDT-SWAP": {"entry_price": 248.50, "timestamp": ...} }
        self.positions: Dict[str, Dict[str, Any]] = {}

        # 根据模式实例化执行器
        self.executor: Optional[BaseExecutor] = None
        self._init_executor()

        if self.executor is not None:
            logger.info(f"OrderRouter 初始化 | mode={self.mode}")
        else:
            logger.info(
                f"OrderRouter 初始化 | mode={self.mode}（只读模式，不发送订单）"
            )

        # 订单历史记录
        self.order_history: list = []

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """异步初始化执行器账户（拉取资金、持仓、交易规则）。"""
        if self.executor is not None:
            try:
                await self.executor.setup_account()
            except Exception:
                logger.exception(f"{self.mode} 执行器初始化失败")

    async def process_signal(
        self,
        ticker: str,
        action: str,
        current_price: float,
        notional_value: float = None,
    ) -> Optional[Dict[str, Any]]:
        """
        处理风控信号并执行下单。

        单向做多互斥逻辑：
          BUY  + 无持仓 → 开多
          BUY  + 已有多 → 忽略
          SELL + 已有多 → 平多
          SELL + 无持仓 → 忽略
          HOLD           → 忽略

        Args:
            ticker:         合约代码（如 TSLA-USDT-SWAP）
            action:         BUY / SELL / HOLD
            current_price:  当前参考价格
            notional_value: 名义价值（None 时使用配置默认值）

        Returns:
            订单结果 dict，无操作返回 None
        """
        action = action.upper().strip()

        # HOLD → 直接忽略
        if action == "HOLD":
            return None

        if action not in ("BUY", "SELL"):
            logger.warning(f"OrderRouter: 无效 action={action}，忽略")
            return None

        if self.executor is None:
            logger.debug(f"OrderRouter: 交易模式={self.mode}，不执行实际下单")
            return None

        if notional_value is None:
            notional_value = self.settings.default_notional_value

        has_position = ticker in self.positions

        # ── BUY 信号 ──
        if action == "BUY":
            if has_position:
                logger.info(
                    f"OrderRouter: {ticker} 已有多头持仓，忽略加仓信号"
                )
                return None
            else:
                return await self._open_long(ticker, current_price, notional_value)

        # ── SELL 信号 ──
        if action == "SELL":
            if has_position:
                return await self._close_long(ticker, current_price, notional_value)
            else:
                logger.info(
                    f"OrderRouter: {ticker} 无多头持仓，忽略平仓信号"
                )
                return None

        return None

    # ------------------------------------------------------------------
    # 内部：开多 / 平多
    # ------------------------------------------------------------------

    async def _open_long(
        self, ticker: str, price: float, notional_value: float
    ) -> Optional[Dict[str, Any]]:
        """
        开立多头仓位。

        流程：
          1. 调用 executor.execute_order(BUY, ...)
          2. 若成交，记录持仓到 self.positions
        """
        logger.info(
            f"OrderRouter: 开多 {ticker} @ {price:.2f} "
            f"notional=${notional_value:,.0f}"
        )

        result = await self.executor.execute_order(
            symbol=ticker,
            action="BUY",
            notional_value=notional_value,
            current_price=price,
        )

        if result:
            self.positions[ticker] = {
                "entry_price": price,
                "timestamp": time.time(),
                "order_id": result.get("order_id", ""),
            }
            self.order_history.append(result)
            logger.info(f"OrderRouter: {ticker} 多头开仓成功")
        else:
            logger.error(f"OrderRouter: {ticker} 多头开仓失败")

        return result

    async def _close_long(
        self, ticker: str, price: float, notional_value: float
    ) -> Optional[Dict[str, Any]]:
        """
        平掉多头仓位。

        流程：
          1. 调用 executor.execute_order(SELL, ...)
          2. 若成交，清除 self.positions 中的记录
        """
        entry = self.positions.get(ticker, {})
        entry_price = entry.get("entry_price", 0)

        pnl_pct = 0.0
        if entry_price > 0:
            pnl_pct = (price - entry_price) / entry_price * 100.0

        logger.info(
            f"OrderRouter: 平多 {ticker} @ {price:.2f} | "
            f"entry={entry_price:.2f} | PnL={pnl_pct:+.2f}%"
        )

        result = await self.executor.execute_order(
            symbol=ticker,
            action="SELL",
            notional_value=notional_value,
            current_price=price,
        )

        if result:
            self.positions.pop(ticker, None)
            # 记录盈亏
            result["pnl_pct"] = round(pnl_pct, 4)
            result["entry_price"] = entry_price
            self.order_history.append(result)
            logger.info(
                f"OrderRouter: {ticker} 多头平仓成功 | PnL={pnl_pct:+.2f}%"
            )
        else:
            logger.error(f"OrderRouter: {ticker} 多头平仓失败")

        return result

    # ------------------------------------------------------------------
    # 内部：执行器工厂
    # ------------------------------------------------------------------

    def _init_executor(self) -> None:
        """根据 trading_mode 初始化对应的执行器。"""
        if self.mode == "PAPER":
            api_key = self.settings.alpaca_api_key
            api_secret = self.settings.alpaca_api_secret
            if not api_key or not api_secret:
                logger.warning(
                    "Alpaca API Key 未配置，PAPER 模式不可用，降级为 OFF"
                )
                self.mode = "OFF"
                return
            self.executor = AlpacaPaperExecutor(api_key, api_secret)

        elif self.mode == "LIVE":
            api_key = self.settings.okx_api_key
            api_secret = self.settings.okx_api_secret
            passphrase = self.settings.okx_passphrase
            if not api_key or not api_secret or not passphrase:
                logger.warning(
                    "OKX API Key 未配置，LIVE 模式不可用，降级为 OFF"
                )
                self.mode = "OFF"
                return
            self.executor = OKXLiveExecutor(api_key, api_secret, passphrase)

        # OFF 模式：executor 保持 None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_position_summary(self) -> Dict[str, Any]:
        """获取持仓摘要。"""
        return {
            "mode": self.mode,
            "positions": dict(self.positions),
            "position_count": len(self.positions),
            "total_orders": len(self.order_history),
        }

    def get_executor_stats(self) -> dict:
        """获取执行器运行统计。"""
        if self.executor and hasattr(self.executor, "stats"):
            return self.executor.stats
        return {}


# ============================================================================
# Mock Demo —— 模拟调用演示
# ============================================================================

async def _run_demo():
    """
    模拟演示：展示 OrderRouter 的完整调用流程。

    实测环境需求：
      - PAPER 模式：需配置 ALPACA_API_KEY / ALPACA_API_SECRET
      - LIVE 模式：需配置 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
      - 无 API Key 时自动降级为 OFF（只读），仅演示逻辑
    """
    print("=" * 64)
    print("  Order Management System — Mock Demo")
    print("=" * 64)

    # 从环境变量加载配置
    from config import get_settings
    settings = get_settings()

    print(f"\n  Trading Mode: {settings.trading_mode}")
    print(f"  Default Notional: ${settings.default_notional_value:,.0f}")

    # 实例化路由器
    router = OrderRouter(settings)

    # 异步初始化（拉取合约面值等）
    print("\n  ── 初始化执行器 ──")
    await router.initialize()

    # 如果模式为 OFF 且无 API Key，演示逻辑仍在，但不会真实发送订单
    print(f"\n  ── 模拟信号处理（{settings.trading_mode} 模式） ──")

    # Demo 1: BUY 信号 → 开多
    print("\n  [Demo 1] BUY TSLA-USDT-SWAP @ $248.50")
    r1 = await router.process_signal(
        ticker="TSLA-USDT-SWAP",
        action="BUY",
        current_price=248.50,
        notional_value=1000.0,
    )
    print(f"    Result: {r1}")
    print(f"    Positions: {router.get_position_summary()}")

    # Demo 2: BUY 信号 → 已有多头，应被忽略
    print("\n  [Demo 2] BUY TSLA-USDT-SWAP @ $249.00（再次买入，应忽略）")
    r2 = await router.process_signal(
        ticker="TSLA-USDT-SWAP",
        action="BUY",
        current_price=249.00,
    )
    print(f"    Result: {r2}  ← None 表示被拦截（已有多头）")

    # Demo 3: SELL 信号 → 平多
    print("\n  [Demo 3] SELL TSLA-USDT-SWAP @ $252.00（平仓）")
    r3 = await router.process_signal(
        ticker="TSLA-USDT-SWAP",
        action="SELL",
        current_price=252.00,
        notional_value=1000.0,
    )
    print(f"    Result: {r3}")
    print(f"    Positions: {router.get_position_summary()}")

    # Demo 4: SELL 信号 → 无持仓，应被忽略
    print("\n  [Demo 4] SELL TSLA-USDT-SWAP @ $251.00（再次卖出，应忽略）")
    r4 = await router.process_signal(
        ticker="TSLA-USDT-SWAP",
        action="SELL",
        current_price=251.00,
    )
    print(f"    Result: {r4}  ← None 表示被拦截（无持仓）")

    # Demo 5: HOLD 信号 → 直接忽略
    print("\n  [Demo 5] HOLD TSLA-USDT-SWAP（忽略）")
    r5 = await router.process_signal(
        ticker="TSLA-USDT-SWAP",
        action="HOLD",
        current_price=250.00,
    )
    print(f"    Result: {r5}  ← None 表示被忽略")

    # ── 摘要 ──
    print("\n" + "=" * 64)
    print("  Demo Summary")
    print("=" * 64)
    print(f"  Mode        : {router.mode}")
    print(f"  Final pos   : {router.get_position_summary()}")
    print(f"  Exec stats  : {router.get_executor_stats()}")
    print(f"  Order log   : {len(router.order_history)} entries")
    print("=" * 64)


if __name__ == "__main__":
    try:
        asyncio.run(_run_demo())
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:
        import traceback
        traceback.print_exc()
