# ===================================================================
# Install dependencies:
#   pip install websockets asyncio aiohttp pandas numpy
#
# Usage:
#   python okx_ai_sector_monitor.py
#
# Description:
#   Interactive OKX AI-sector order-book momentum monitor.
#   Select a sub-sector via terminal, then watch live OBI/ROC alerts.
# ===================================================================

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import websockets

# ============================================================================
# Configuration
# ============================================================================

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"

# -------------------- AI Sector Classification --------------------
AI_SECTORS: Dict[str, Dict] = {
    "1": {
        "name": "DePIN / Compute  (去中心化算力与云)",
        "tokens": ["RNDR-USDT", "TAO-USDT", "AKT-USDT", "IO-USDT"],
    },
    "2": {
        "name": "AI Agents / Models (AI代理与大模型)",
        "tokens": ["FET-USDT", "WLD-USDT", "PHB-USDT", "ARKM-USDT"],
    },
    "3": {
        "name": "Data / Infrastructure (数据网络与基础设施)",
        "tokens": ["GRT-USDT", "THETA-USDT", "OCEAN-USDT"],
    },
}

# Build reverse lookup: token -> sector_key
_TOKEN_SECTOR: Dict[str, str] = {}
for _k, _v in AI_SECTORS.items():
    for _t in _v["tokens"]:
        _TOKEN_SECTOR[_t] = _k

# -------------------- Tuning Parameters --------------------
OBI_ALERT_THRESHOLD = 0.6
OBI_WINDOW_SECONDS = 10.0
STATUS_INTERVAL = 5.0
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0

# ============================================================================
# ANSI Terminal Colors
# ============================================================================

class TC:
    RST = "\033[0m"
    BLD = "\033[1m"
    RED = "\033[91m"
    GRN = "\033[92m"
    YEL = "\033[93m"
    BLU = "\033[94m"
    MAG = "\033[95m"
    CYN = "\033[96m"
    WHT = "\033[97m"
    BG_R = "\033[41m"
    BG_G = "\033[42m"

# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class OBIRecord:
    ts: float
    obi: float
    bid_vol: float
    ask_vol: float

@dataclass
class TokenBook:
    symbol: str
    sector_key: str
    sector_name: str
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    mid_price: float = 0.0
    obi_history: List[OBIRecord] = field(default_factory=list)
    last_ts: float = 0.0

# ============================================================================
# Asynchronous Input Helper
# ============================================================================

async def async_input(prompt: str = "") -> str:
    """Non-blocking read from stdin using a thread."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = await asyncio.to_thread(sys.stdin.readline)
    return line.rstrip("\n")

# ============================================================================
# OKX AI Sector Monitor
# ============================================================================

class OKXAIBlockMonitor:
    """
    Interactive OKX WebSocket monitor for AI-sector order-book momentum.

    Steps:
      1. Print the three AI sub-sectors and prompt the user for a choice.
      2. Subscribe to ``books5`` (top-5 depth) for the selected sector's tokens.
      3. Compute OBI + ROC in real time; emit colourised terminal alerts.
    """

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._alive = False
        self._backoff = RECONNECT_BASE_DELAY
        self.symbols: List[str] = []
        self.books: Dict[str, TokenBook] = {}
        self._setup_logging()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _setup_logging(self):
        self.log = logging.getLogger("aimon")
        self.log.setLevel(logging.INFO)
        if not self.log.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter(
                f"{TC.CYN}[%(asctime)s]{TC.RST} %(message)s",
                datefmt="%H:%M:%S"
            ))
            self.log.addHandler(h)

    # ==================================================================
    # Terminal interaction (non-blocking)
    # ==================================================================

    async def select_sector(self) -> str:
        """Print the sector menu and return the chosen key (e.g. '1')."""
        print(f"\n{TC.BLD}{TC.CYN}{'=' * 56}{TC.RST}")
        print(f"{TC.BLD}{TC.CYN}   OKX AI-Crypto Sector Order-Book Momentum Monitor{TC.RST}")
        print(f"{TC.BLD}{TC.CYN}{'=' * 56}{TC.RST}\n")

        for key in sorted(AI_SECTORS.keys(), key=int):
            sec = AI_SECTORS[key]
            tokens_str = ", ".join(sec["tokens"])
            print(f"  {TC.BLD}[{key}]{TC.RST} {TC.WHT}{sec['name']}{TC.RST}")
            print(f"     {TC.BLU}Tokens:{TC.RST} {TC.YEL}{tokens_str}{TC.RST}\n")

        while True:
            choice = await async_input(
                f"\n{TC.BLD}Select a sector to monitor [1/2/3]:{TC.RST} "
            )
            if choice.strip() in AI_SECTORS:
                return choice.strip()
            print(f"{TC.RED}Invalid choice '{choice}'. Please enter 1, 2 or 3.{TC.RST}")

    def _init_books(self, sector_key: str):
        """Create TokenBook entries for the chosen sector."""
        sec = AI_SECTORS[sector_key]
        self.symbols = sec["tokens"]
        self.books = {}
        for sym in self.symbols:
            self.books[sym] = TokenBook(
                symbol=sym,
                sector_key=sector_key,
                sector_name=sec["name"],
            )

    # ==================================================================
    # WebSocket lifecycle
    # ==================================================================

    async def connect_loop(self):
        """Connect to OKX with exponential-backoff reconnection."""
        while self._alive:
            try:
                self.log.info("Connecting to OKX public channel …")
                async with websockets.connect(
                    OKX_WS_PUBLIC,
                    ping_interval=None,
                    close_timeout=5,
                    max_size=2 ** 20,
                ) as sock:
                    self.ws = sock
                    self._backoff = RECONNECT_BASE_DELAY
                    self.log.info(f"{TC.GRN}WebSocket connected{TC.RST}")

                    await self._subscribe(sock)
                    await self._recv_loop(sock)

            except (websockets.ConnectionClosed, OSError, TimeoutError) as e:
                self.log.warning(
                    f"{TC.YEL}Disconnected: {e}.  "
                    f"Reconnecting in {self._backoff:.1f}s …{TC.RST}"
                )
            except Exception as e:
                self.log.error(f"Unhandled connection error: {e}")

            if self._alive:
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 1.5, RECONNECT_MAX_DELAY)

    async def _subscribe(self, sock):
        args = [{"channel": "books5", "instId": s} for s in self.symbols]
        await sock.send(json.dumps({"op": "subscribe", "args": args}))
        self.log.info(f"Subscribed {len(self.symbols)} symbols: {', '.join(self.symbols)}")

    async def _recv_loop(self, sock):
        """Receive & dispatch inbound frames."""
        async for raw in sock:
            raw_text = raw if isinstance(raw, str) else raw.decode()

            if raw_text.strip() == "ping":
                await sock.send("pong")
                continue

            try:
                msg = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            event = msg.get("event")
            if event == "error":
                self.log.error("API error: %s", msg.get("msg", msg))
                continue

            arg = msg.get("arg", {})
            if arg.get("channel") == "books5":
                await self._on_books5(arg["instId"], msg)

    # ==================================================================
    # Order-book processing
    # ==================================================================

    async def _on_books5(self, symbol: str, msg: dict):
        book = self.books.get(symbol)
        if not book:
            return

        data = msg.get("data", [])
        if not data:
            return
        entry = data[0]

        bids = [(float(b[0]), float(b[1])) for b in entry.get("bids", [])]
        asks = [(float(a[0]), float(a[1])) for a in entry.get("asks", [])]

        if not bids or not asks:
            return

        book.bids = bids
        book.asks = asks
        book.mid_price = (bids[0][0] + asks[0][0]) / 2.0
        book.last_ts = time.time()

        # ---- OBI ----
        bid_qty = sum(b[1] for b in bids)
        ask_qty = sum(a[1] for a in asks)
        total = bid_qty + ask_qty
        obi = (bid_qty - ask_qty) / total if total > 0 else 0.0

        rec = OBIRecord(ts=time.time(), obi=obi, bid_vol=bid_qty, ask_vol=ask_qty)
        book.obi_history.append(rec)

        # Prune stale records
        cutoff = time.time() - OBI_WINDOW_SECONDS * 3
        book.obi_history = [r for r in book.obi_history if r.ts > cutoff]

        await self._check_alert(symbol, book, rec)

    # ==================================================================
    # Momentum analysis (ROC)
    # ==================================================================

    def _calc_roc(self, book: TokenBook) -> Optional[float]:
        """Rate of change of OBI (per second) over the rolling window."""
        history = book.obi_history
        if len(history) < 3:
            return None

        now = time.time()
        window = [r for r in history if r.ts >= now - OBI_WINDOW_SECONDS]
        if len(window) < 2:
            return None

        t = np.array([r.ts - window[0].ts for r in window])
        o = np.array([r.obi for r in window])
        if t[-1] == 0:
            return None
        return float((o[-1] - o[0]) / t[-1])

    # ==================================================================
    # Alert engine
    # ==================================================================

    async def _check_alert(self, symbol: str, book: TokenBook, rec: OBIRecord):
        if abs(rec.obi) < OBI_ALERT_THRESHOLD:
            return

        roc = self._calc_roc(book)
        if roc is None or abs(roc) < 0.005:
            return

        direction = "BUY " if rec.obi > 0 else "SELL"
        color = TC.GRN if rec.obi > 0 else TC.RED
        bg = TC.BG_G if rec.obi > 0 else TC.BG_R
        ts = time.strftime("%H:%M:%S", time.localtime())

        self.log.info(
            f"{bg}{TC.BLD} ALERT {TC.RST} "
            f"[{TC.MAG}{book.sector_name}{TC.RST}] "
            f"{TC.MAG}{symbol:<12}{TC.RST}"
            f"{TC.WHT}{ts}{TC.RST} | "
            f"PX:{TC.CYN}{book.mid_price:.4f}{TC.RST} | "
            f"DIR:{color}{TC.BLD}{direction}{TC.RST} | "
            f"OBI:{color}{rec.obi:+.4f}{TC.RST} | "
            f"ROC:{color}{roc:+.4f}/s{TC.RST} | "
            f"B:{TC.GRN}{rec.bid_vol:.1f}{TC.RST} "
            f"A:{TC.RED}{rec.ask_vol:.1f}{TC.RST}"
        )

    # ==================================================================
    # Periodic status table
    # ==================================================================

    async def _status_loop(self):
        while self._alive:
            await asyncio.sleep(STATUS_INTERVAL)

            sec = AI_SECTORS.get(self.symbols[0] and _TOKEN_SECTOR.get(self.symbols[0], ""), {})
            sector_label = sec.get("name", "Unknown") if sec else "Unknown"

            header = (
                f"\n{TC.BLU}{'─' * 86}{TC.RST}\n"
                f"{TC.BLD}  Sector: {TC.MAG}{sector_label}{TC.RST}\n"
                f"{TC.BLD}  {'Symbol':<12} {'Price':>10} {'OBI':>8} {'Dir':>6} "
                f"{'BidVol':>10} {'AskVol':>10} {'ROC/s':>8}{TC.RST}\n"
                f"{TC.BLU}{'─' * 86}{TC.RST}"
            )
            print(header)

            for symbol, book in self.books.items():
                if not book.obi_history:
                    continue
                rec = book.obi_history[-1]
                obi = rec.obi

                if obi > 0.03:
                    dc, dname = TC.GRN, "BUY"
                elif obi < -0.03:
                    dc, dname = TC.RED, "SELL"
                else:
                    dc, dname = TC.YEL, "─"

                roc = self._calc_roc(book) or 0.0

                print(
                    f"  {symbol:<12} "
                    f"{book.mid_price:>10.4f} "
                    f"{dc}{obi:>+7.4f}{TC.RST} "
                    f"{dc}{dname:>6}{TC.RST} "
                    f"{rec.bid_vol:>10.1f} "
                    f"{rec.ask_vol:>10.1f} "
                    f"{roc:>+8.4f}"
                )

            print(f"{TC.BLU}{'─' * 86}{TC.RST}\n")

    # ==================================================================
    # Public API
    # ==================================================================

    async def start(self):
        # 1. Interactive sector selection (non-blocking)
        sector_key = await self.select_sector()
        self._init_books(sector_key)

        sec = AI_SECTORS[sector_key]
        self._alive = True
        self.log.info(
            f"\n{TC.BLD}{TC.CYN}"
            f"╔══════════════════════════════════════════════╗\n"
            f"║  Monitoring: {sec['name']:<34s} ║\n"
            f"║  Tokens: {', '.join(self.symbols)}\n"
            f"║  OBI Threshold: {OBI_ALERT_THRESHOLD}                         ║\n"
            f"║  ROC Window: {OBI_WINDOW_SECONDS}s                          ║\n"
            f"╚══════════════════════════════════════════════╝"
            f"{TC.RST}\n"
        )

        # 2. Run WebSocket + status display concurrently
        await asyncio.gather(
            self.connect_loop(),
            self._status_loop(),
        )

    async def stop(self):
        self._alive = False
        if self.ws:
            await self.ws.close()
        self.log.info("Monitor stopped.")


# ============================================================================
# Entry point
# ============================================================================

async def main():
    monitor = OKXAIBlockMonitor()
    try:
        await monitor.start()
    except KeyboardInterrupt:
        print(f"\n{TC.YEL}Interrupted — shutting down …{TC.RST}")
        await monitor.stop()
    except Exception as exc:
        logging.error("Fatal: %s", exc, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
