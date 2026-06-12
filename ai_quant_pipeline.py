# ===================================================================
# Install dependencies:
#   pip install alpaca-py openai pandas python-dotenv websockets
#
# Usage:
#   1) Copy .env.example below into a file named ".env", fill in your keys:
#        ALPACA_API_KEY=PK...
#        ALPACA_SECRET_KEY=...
#   2) Make sure Ollama is running locally with the llama3.1 model pulled:
#        ollama pull llama3.1
#   3) python ai_quant_pipeline.py
#
# Description:
#   Real-time Alpaca quote stream → TOBI trigger → local Ollama decision
#   → Alpaca paper-trading execution.  Covers AI supply-chain stocks.
# ===================================================================

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from dotenv import load_dotenv
from openai import AsyncOpenAI

from alpaca.data.live import StockDataStream
from alpaca.data.models import Quote
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# Ollama
OLLAMA_BASE_URL   = "http://localhost:11434/v1"
OLLAMA_MODEL      = "llama3.1"

# Trading
TRADE_NOTIONAL    = 1000.0          # USD per order (fractional shares)
COOLDOWN_SECONDS  = 300             # 5 min per symbol

# TOBI thresholds
TOBI_EXTREME_LONG  = 0.8
TOBI_EXTREME_SHORT = -0.8

# ============================================================================
# AI Supply-Chain Sectors (tokenized stocks on Alpaca)
# ============================================================================

AI_SECTORS: Dict[str, Dict] = {
    "1": {
        "name": "Upstream — Chips & Hardware",
        "symbols": ["NVDA", "AMD", "TSM", "ASML"],
    },
    "2": {
        "name": "Midstream — Cloud & LLMs  ",
        "symbols": ["MSFT", "GOOGL", "AMZN", "META"],
    },
    "3": {
        "name": "Downstream — AI Apps & Data",
        "symbols": ["PLTR", "CRWD", "SNOW"],
    },
}

_SYMBOL_SECTOR: Dict[str, str] = {}
for _k, _v in AI_SECTORS.items():
    for _s in _v["symbols"]:
        _SYMBOL_SECTOR[_s] = _k

# ============================================================================
# ANSI Terminal Colors
# ============================================================================

class TC:
    RST = "\033[0m";   BLD = "\033[1m"
    RED = "\033[91m";  GRN = "\033[92m"
    YEL = "\033[93m";  BLU = "\033[94m"
    MAG = "\033[95m";  CYN = "\033[96m"
    WHT = "\033[97m"
    BG_R = "\033[41m"; BG_G = "\033[42m"

# ============================================================================
# Async Input Helper
# ============================================================================

async def async_input(prompt: str = "") -> str:
    sys.stdout.write(prompt); sys.stdout.flush()
    return (await asyncio.to_thread(sys.stdin.readline)).rstrip("\n")

# ============================================================================
# Shared Logging
# ============================================================================

def _make_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            f"{TC.CYN}[%(asctime)s]{TC.RST} %(message)s", datefmt="%H:%M:%S"
        ))
        log.addHandler(h)
    return log

# ============================================================================
# 1.  AI Decision Engine  (Ollama via OpenAI-compatible API)
# ============================================================================

SYSTEM_PROMPT = """\
You are a cold, calculating quantitative risk-controller for an automated
trading system.  You receive a TOBI (Top-of-Book Imbalance) signal that
indicates extreme buy or sell pressure.  You must decide whether to trade.

Rules:
- Only output a single token: APPROVE or REJECT.
- Do NOT output any explanation, punctuation, or whitespace beyond that token.
- APPROVE means the signal is credible enough for a $1000 notional market order.
- REJECT means the signal is likely noise or carries unacceptable risk.
"""

class AIDecisionEngine:
    """Calls local Ollama (llama3.1) for a fast APPROVE / REJECT decision."""

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",
        )
        self.model = OLLAMA_MODEL
        self.log = _make_logger("ai")

    async def decide(self, symbol: str, tobi: float, bid_vol: float,
                     ask_vol: float, price: float) -> bool:
        """
        Return True if the local LLM returns 'APPROVE', else False.
        """
        user_msg = (
            f"Symbol: {symbol}\n"
            f"TOBI: {tobi:.4f}  (bid_size={bid_vol:.0f}  ask_size={ask_vol:.0f})\n"
            f"Mid price: {price:.2f}\n"
            f"Direction: {'BUY' if tobi > 0 else 'SELL'}"
        )

        self.log.info(
            f"{TC.YEL}{TC.BLD}  |> Llama 3.1 inferring …{TC.RST}  "
            f"symbol={TC.MAG}{symbol}{TC.RST}  tobi={tobi:+.4f}"
        )

        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=10,
                ),
                timeout=8.0,          # fail fast – don't stall the pipeline
            )
        except asyncio.TimeoutError:
            self.log.warning(f"{TC.RED}  |> Llama 3.1 timed out → REJECT{TC.RST}")
            return False
        except Exception as exc:
            self.log.error(f"{TC.RED}  |> Llama 3.1 call failed: {exc}{TC.RST}")
            return False

        raw = resp.choices[0].message.content.strip().upper() if resp.choices else ""
        approved = (raw == "APPROVE")

        verdict_color = TC.GRN if approved else TC.RED
        self.log.info(
            f"{verdict_color}{TC.BLD}  |> Llama verdict: {raw}{TC.RST}"
        )
        return approved

# ============================================================================
# 2.  Paper Trading Executor  (Alpaca Paper API)
# ============================================================================

class PaperTrader:
    """Submits market orders to Alpaca's paper-trading endpoint."""

    def __init__(self, api_key: str, secret_key: str):
        self.client = TradingClient(api_key, secret_key, paper=True)
        self.log = _make_logger("trade")

    async def submit(self, symbol: str, side: OrderSide) -> Optional[str]:
        """
        Place a $TRADE_NOTIONAL market order.  Returns the order ID or None.
        """
        req = MarketOrderRequest(
            symbol=symbol,
            notional=TRADE_NOTIONAL,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = await asyncio.to_thread(self.client.submit_order, req)
            self.log.info(
                f"{TC.BLD}{TC.GRN}  |> ORDER PLACED{TC.RST}  "
                f"id={TC.WHT}{order.id}{TC.RST}  "
                f"{symbol}  {side.value}  ${TRADE_NOTIONAL:.0f} notional"
            )
            return str(order.id)
        except Exception as exc:
            self.log.error(
                f"{TC.BG_R}  |> ORDER FAILED{TC.RST}  "
                f"{symbol}  {side.value}  —  {exc}"
            )
            return None

# ============================================================================
# 3.  Main Trading Pipeline
# ============================================================================

@dataclass
class TOBISnapshot:
    symbol: str
    tobi: float
    bid_size: float
    ask_size: float
    price: float
    ts: float

class TradingPipeline:
    """
    Orchestrates:  quote stream → TOBI trigger → AI decision → paper trade.
    """

    def __init__(self):
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            print(f"{TC.BG_R}ERROR: Set ALPACA_API_KEY / ALPACA_SECRET_KEY in .env{TC.RST}")
            sys.exit(1)

        self.ai     = AIDecisionEngine()
        self.trader = PaperTrader(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self.stream = StockDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)

        self.symbols: List[str]          = []
        self._alive: bool                = False
        self._cooldowns: Dict[str, float] = {}       # symbol → last trigger epoch
        self._latest: Dict[str, TOBISnapshot] = {}    # latest TOBI per symbol

        self.log = _make_logger("pipeline")

        # Stats
        self._trigger_count = 0
        self._trade_count   = 0

    # ------------------------------------------------------------------
    # Sector selection
    # ------------------------------------------------------------------

    async def select_sector(self) -> str:
        print(f"\n{TC.BLD}{TC.CYN}{'=' * 56}{TC.RST}")
        print(f"{TC.BLD}{TC.CYN}   AI Quant Pipeline — Alpaca + Ollama{TC.RST}")
        print(f"{TC.BLD}{TC.CYN}{'=' * 56}{TC.RST}\n")

        for key in sorted(AI_SECTORS.keys(), key=int):
            sec  = AI_SECTORS[key]
            syms = ", ".join(sec["symbols"])
            print(f"  {TC.BLD}[{key}]{TC.RST} {TC.WHT}{sec['name']}{TC.RST}")
            print(f"     {TC.BLU}Stocks:{TC.RST} {TC.YEL}{syms}{TC.RST}\n")

        while True:
            choice = await async_input(
                f"{TC.BLD}Select sector [1/2/3]:{TC.RST} "
            )
            if choice.strip() in AI_SECTORS:
                return choice.strip()
            print(f"{TC.RED}Invalid. Enter 1, 2, or 3.{TC.RST}")

    # ------------------------------------------------------------------
    # Quote handler — called by alpaca-py for every tick
    # ------------------------------------------------------------------

    async def _on_quote(self, quote: Quote):
        symbol = quote.symbol
        if symbol not in self.symbols:
            return

        bid_sz = float(quote.bid_size)
        ask_sz = float(quote.ask_size)
        total  = bid_sz + ask_sz
        tobi   = (bid_sz - ask_sz) / total if total > 0 else 0.0
        mid_px = (quote.bid_price + quote.ask_price) / 2.0

        snap = TOBISnapshot(
            symbol=symbol,
            tobi=tobi,
            bid_size=bid_sz,
            ask_size=ask_sz,
            price=mid_px,
            ts=time.time(),
        )
        self._latest[symbol] = snap

        # ---- Check extreme TOBI ----
        if not (tobi >= TOBI_EXTREME_LONG or tobi <= TOBI_EXTREME_SHORT):
            return

        # ---- Cooldown check ----
        now = time.time()
        last = self._cooldowns.get(symbol, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        self._cooldowns[symbol] = now

        # ---- TRIGGER ----
        self._trigger_count += 1
        direction_label = "LONG" if tobi > 0 else "SHORT"
        color = TC.GRN if tobi > 0 else TC.RED
        sector_key = _SYMBOL_SECTOR.get(symbol, "?")
        sector_name = AI_SECTORS.get(sector_key, {}).get("name", "Unknown")

        self.log.info(
            f"{TC.BG_Y}{TC.BLD} TRIGGER {TC.RST} "
            f"[{TC.MAG}{sector_name}{TC.RST}] "
            f"{TC.MAG}{symbol}{TC.RST}  "
            f"TOBI={color}{tobi:+.4f}{TC.RST}  "
            f"PX={TC.CYN}{mid_px:.2f}{TC.RST}  "
            f"Dir={color}{TC.BLD}{direction_label}{TC.RST}  "
            f"B={TC.GRN}{bid_sz:.0f}{TC.RST}  "
            f"A={TC.RED}{ask_sz:.0f}{TC.RST}"
        )

        # ---- AI Decision ----
        approved = await self.ai.decide(symbol, tobi, bid_sz, ask_sz, mid_px)

        if not approved:
            self.log.info(f"{TC.YEL}  |> AI rejected — no trade{TC.RST}")
            return

        # ---- Execute ----
        side = OrderSide.BUY if tobi > 0 else OrderSide.SELL
        order_id = await self.trader.submit(symbol, side)
        if order_id:
            self._trade_count += 1

    # ------------------------------------------------------------------
    # Status reporter
    # ------------------------------------------------------------------

    async def _status_loop(self):
        while self._alive:
            await asyncio.sleep(10.0)

            if not self._latest:
                continue

            first_sym = self.symbols[0] if self.symbols else ""
            sec_name  = AI_SECTORS.get(_SYMBOL_SECTOR.get(first_sym, ""), {}).get("name", "")

            header = (
                f"\n{TC.BLU}{'─' * 78}{TC.RST}\n"
                f"{TC.BLD}  Sector: {TC.MAG}{sec_name}{TC.RST}  |  "
                f"Triggers: {self._trigger_count}  |  Trades: {self._trade_count}\n"
                f"{TC.BLD}  {'Symbol':<8} {'Price':>10} {'TOBI':>8} {'BidSz':>10} "
                f"{'AskSz':>10} {'Cooldown':>12}{TC.RST}\n"
                f"{TC.BLU}{'─' * 78}{TC.RST}"
            )
            print(header)

            now = time.time()
            for sym in self.symbols:
                snap = self._latest.get(sym)
                if snap is None:
                    continue
                cdl = COOLDOWN_SECONDS - (now - self._cooldowns.get(sym, 0))
                cdl_str = f"{max(0, cdl):.0f}s" if cdl > 0 else "READY"

                if snap.tobi > 0.2:
                    c = TC.GRN
                elif snap.tobi < -0.2:
                    c = TC.RED
                else:
                    c = TC.YEL

                print(
                    f"  {sym:<8} "
                    f"{snap.price:>10.2f} "
                    f"{c}{snap.tobi:>+7.4f}{TC.RST} "
                    f"{snap.bid_size:>10.0f} "
                    f"{snap.ask_size:>10.0f} "
                    f"{cdl_str:>12}"
                )

            print(f"{TC.BLU}{'─' * 78}{TC.RST}\n")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        sector_key = await self.select_sector()
        sec = AI_SECTORS[sector_key]
        self.symbols = sec["symbols"]

        self._alive = True
        self.log.info(
            f"\n{TC.BLD}{TC.CYN}"
            f"╔══════════════════════════════════════════════╗\n"
            f"║  Pipeline active — {sec['name']}\n"
            f"║  Symbols: {', '.join(self.symbols)}\n"
            f"║  TOBI threshold: ±{TOBI_EXTREME_LONG}  |  "
            f"Cooldown: {COOLDOWN_SECONDS}s  |  "
            f"Notional: ${TRADE_NOTIONAL:.0f}\n"
            f"║  Ollama: {OLLAMA_MODEL} @ {OLLAMA_BASE_URL}\n"
            f"╚══════════════════════════════════════════════╝"
            f"{TC.RST}\n"
        )

        # Register the async quote handler
        self.stream.subscribe_quotes(self._on_quote, *self.symbols)

        # Run stream + status reporter concurrently
        await asyncio.gather(
            self.stream._run_forever(),
            self._status_loop(),
        )

    async def stop(self):
        self._alive = False
        try:
            await self.stream.stop()
        except Exception:
            pass
        self.log.info("Pipeline stopped.")


# ============================================================================
# Entry point
# ============================================================================

async def main():
    pipeline = TradingPipeline()
    try:
        await pipeline.start()
    except KeyboardInterrupt:
        print(f"\n{TC.YEL}Interrupted — shutting down …{TC.RST}")
        await pipeline.stop()
    except Exception as exc:
        logging.error("Fatal: %s", exc, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
