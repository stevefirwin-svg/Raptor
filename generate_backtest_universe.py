"""
generate_backtest_universe.py
-----------------------------
Generates backtest_universe.txt -- a fixed, broad symbol list for
reproducible backtesting. Run this ONCE and commit the result.

Design rationale:
- The backtest must use a stable universe that does not shift with
  live market conditions. Using today's live screen produces different
  results every run -- making calibration and regression testing
  meaningless.
- The list is drawn from S&P 500 + NASDAQ 100 constituents plus
  historically liquid mid-caps. This approximates the opportunity set
  available to a swing trader over 2020-2025.
- Survivorship bias is partially mitigated by including stocks that
  were large in 2020 but may have since declined (e.g. PYPL, NFLX,
  SNAP). A truly bias-free universe would use point-in-time index
  membership -- that requires a commercial data provider.

Usage:
    python generate_backtest_universe.py
    # Then commit backtest_universe.txt to GitHub -- never delete it.

Re-run only when:
    - You want to expand/contract the opportunity set deliberately
    - You are starting a new multi-year backtest period
    - You document WHY the universe changed in the commit message
"""

import os
import datetime

# -- BROAD HISTORICALLY-REPRESENTATIVE UNIVERSE --------------------------------
# ~150 symbols covering: mega-cap tech, financials, healthcare, industrials,
# consumer, energy, semis, biotech, growth mid-caps.
# Chosen for consistent liquidity 2020-2025 (>$10M ADV throughout).

UNIVERSE = sorted(set([
    # Mega-cap tech / FAANG+
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    "NFLX", "ADBE", "CRM", "ORCL", "INTC", "QCOM", "TXN", "AVGO",
    "AMD", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "SNPS", "CDNS",

    # Software / Cloud
    "NOW", "WDAY", "TEAM", "DDOG", "SNOW", "NET", "ZS", "CRWD",
    "OKTA", "MDB", "GTLB", "HUBS", "BILL", "SPLK", "VEEV", "COUP",

    # Fintech / Payments
    "V", "MA", "PYPL", "SQ", "AFRM", "SOFI", "UPST", "LC",
    "GPN", "FIS", "FISV", "AXP",

    # Large-cap financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW",
    "USB", "PNC", "TFC", "COF", "AIG", "MET", "PRU",

    # Healthcare / Pharma / Biotech
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "BMY", "AMGN",
    "GILD", "BIIB", "REGN", "VRTX", "MRNA", "BNTX", "IDXX", "ISRG",
    "SYK", "BSX", "MDT", "ABT", "TMO", "DHR", "IQV",

    # Consumer discretionary
    "AMZN", "TSLA", "HD", "LOW", "TGT", "WMT", "COST", "NKE",
    "SBUX", "MCD", "CMG", "YUM", "DPZ", "ROST", "TJX", "LULU",
    "ABNB", "BKNG", "EXPE", "MAR", "HLT",

    # Communication / Media
    "DIS", "CMCSA", "PARA", "WBD", "FOX", "TTWO", "EA", "ATVI",
    "SPOT", "PINS", "SNAP", "TWTR",  # TWTR pre-acquisition

    # Industrials / Defense
    "BA", "RTX", "LMT", "GD", "NOC", "GE", "HON", "MMM",
    "CAT", "DE", "EMR", "ETN", "PH", "ROK", "ITW", "XYL",
    "UPS", "FDX", "CSX", "NSC", "UNP",

    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "HAL", "MPC", "PSX",
    "VLO", "PXD", "DVN", "FANG",

    # Real estate / Utilities (lower but included for regime diversity)
    "AMT", "PLD", "CCI", "EQIX", "SPG", "O",
    "NEE", "DUK", "SO", "AEP",

    # Growth / High-beta (important for momentum capture 2020-2021)
    "COIN", "HOOD", "SHOP", "UBER", "LYFT", "DASH", "RBLX",
    "PLTR", "ASTR", "PATH", "MNDY", "GTLB", "AI", "BBAI",
    "ZM", "DOCU", "PTON", "ROKU", "TTD", "PUBM", "MGNI",

    # SPY for benchmark (always included, filtered out in signal engine)
    "SPY",
]))

# Remove known bad tickers (acquired, delisted, changed symbols)
REMOVE = {"TWTR", "ATVI", "COUP", "PARA"}  # Acquired during period
UNIVERSE = [s for s in UNIVERSE if s not in REMOVE]
UNIVERSE = sorted(UNIVERSE)

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "backtest_universe.txt")

header = f"""# Raptor Backtest Universe -- LOCKED
# Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
# Symbols: {len(UNIVERSE)}
#
# DO NOT edit manually. Re-run generate_backtest_universe.py if changes needed.
# Commit this file. Never delete it. Document any regeneration in git commit.
#
# Coverage: S&P 500 / NASDAQ 100 core + historically liquid mid-caps
# Period designed for: 2020-01-01 to 2025-12-31
# ---------------------------------------------------------------------
"""

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write(header)
    for sym in UNIVERSE:
        f.write(sym + "\n")

print(f"✅ backtest_universe.txt written: {len(UNIVERSE)} symbols")
print(f"   Path: {OUTPUT_FILE}")
print()
print("Next steps:")
print("  1. git add backtest_universe.txt")
print("  2. git commit -m 'Add locked backtest universe: {len(UNIVERSE)} symbols'")
print("  3. python backtest.py --no-gap1   # should now use locked universe")
