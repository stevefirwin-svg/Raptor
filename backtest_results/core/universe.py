"""
RAPTOR UNIVERSE v4.0  —  core/universe.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every symbol has sector classification for:
  - Portfolio concentration limits (max 50% in any sector)
  - Correlation management (same-sector stocks correlate ~0.6-0.8)
  - Regime-aware sector rotation

Universe criteria:
  Market cap  > $10B
  ADV         > 2M shares/day
  Options liquid (for flow signal estimation)
  No meme stocks, no SPAC wrecks, no crypto-equity proxies
"""


SECTOR_MAP = {
    # ── Semiconductor ─────────────────────────────────────────────────────────
    "NVDA":  "SEMICONDUCTOR",
    "AMD":   "SEMICONDUCTOR",
    "TSM":   "SEMICONDUCTOR",
    "AVGO":  "SEMICONDUCTOR",
    "AMAT":  "SEMICONDUCTOR",
    "LRCX":  "SEMICONDUCTOR",

    # ── Big Tech / Software ───────────────────────────────────────────────────
    "MSFT":  "TECH",
    "AAPL":  "TECH",
    "META":  "TECH",
    "GOOGL": "TECH",
    "AMZN":  "TECH",

    # ── High-Beta Growth ──────────────────────────────────────────────────────
    "TSLA":  "GROWTH",
    "PLTR":  "GROWTH",
    "NET":   "GROWTH",

    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM":   "ENERGY",
    "CVX":   "ENERGY",
    "COP":   "ENERGY",
    "EOG":   "ENERGY",
    "VLO":   "ENERGY",
    "MPC":   "ENERGY",
    "OXY":   "ENERGY",

    # ── Healthcare ────────────────────────────────────────────────────────────
    "UNH":   "HEALTHCARE",
    "LLY":   "HEALTHCARE",
    "ABBV":  "HEALTHCARE",

    # ── Industrials / Defense ─────────────────────────────────────────────────
    "CAT":   "INDUSTRIAL",
    "DE":    "INDUSTRIAL",
    "RTX":   "DEFENSE",
    "GD":    "DEFENSE",

    # ── Consumer ──────────────────────────────────────────────────────────────
    "SBUX":  "CONSUMER",
    "MCD":   "CONSUMER",

    # ── ETFs ──────────────────────────────────────────────────────────────────
    "SPY":   "INDEX",
    "QQQ":   "INDEX",
    "XLE":   "INDEX",

    # ── Crypto ────────────────────────────────────────────────────────────────
    "BTC/USD": "CRYPTO",
    "ETH/USD": "CRYPTO",
}


def get_trade_universe() -> list:
    """Return full trading universe."""
    return list(SECTOR_MAP.keys())


def get_sector(ticker: str) -> str:
    """Return sector for a given ticker."""
    return SECTOR_MAP.get(ticker, "UNKNOWN")


def is_crypto(ticker: str) -> bool:
    """Check if ticker is crypto."""
    return "/" in ticker or ticker in ("BTCUSD", "ETHUSD")


def get_sector_tickers(sector: str) -> list:
    """Return all tickers in a given sector."""
    return [t for t, s in SECTOR_MAP.items() if s == sector]


def get_stocks_only() -> list:
    """Return non-crypto, non-ETF tickers."""
    return [t for t, s in SECTOR_MAP.items()
            if s not in ("CRYPTO", "INDEX")]
