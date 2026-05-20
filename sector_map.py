"""
sector_map.py — Symbol → Sector Classification
================================================
HOLE 14 FIX: config has max_sector_pct=0.30 but no code ever enforced it.
This module provides sector lookup used by main.py at entry time.

Approach:
1. Hardcoded map for the ~200 most common Raptor universe symbols.
   Covers S&P 500 / NASDAQ 100 / Russell 1000 overlap — the universe
   universe_builder.py screens from.
2. Correlation-to-sector-ETF fallback for unknown symbols:
   Assign to the sector ETF with highest 20-day return correlation.
   Uses bars already in memory — no new API calls.

Sector ETF proxies (same as macro_context.py breadth calculation):
  XLK  → Technology
  XLF  → Financials
  XLE  → Energy
  XLV  → Healthcare
  XLI  → Industrials
  XLY  → ConsumerDiscretionary
  XLP  → ConsumerStaples
  XLU  → Utilities
  XLB  → Materials
  XLRE → RealEstate
  XLC  → Communication
"""

from typing import Dict, Optional
import numpy as np

# ── Hardcoded sector map ──────────────────────────────────────────────────────
# Covers the bulk of the Raptor universe. Unknown symbols fall through to
# correlation-based classification.

SECTOR_MAP: Dict[str, str] = {
    # Technology
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology","AMD":"Technology",
    "AVGO":"Technology","ORCL":"Technology","CRM":"Technology","ADBE":"Technology",
    "INTC":"Technology","QCOM":"Technology","TXN":"Technology","MU":"Technology",
    "AMAT":"Technology","LRCX":"Technology","KLAC":"Technology","MRVL":"Technology",
    "SNPS":"Technology","CDNS":"Technology","ANSS":"Technology","FTNT":"Technology",
    "PANW":"Technology","CRWD":"Technology","ZS":"Technology","OKTA":"Technology",
    "DDOG":"Technology","SNOW":"Technology","MDB":"Technology","NET":"Technology",
    "VEEV":"Technology","NOW":"Technology","WDAY":"Technology","TEAM":"Technology",
    "ZM":"Technology","DOCU":"Technology","SHOP":"Technology","TWLO":"Technology",
    "HUBS":"Technology","U":"Technology","RBLX":"Technology","COIN":"Technology",
    "MSTR":"Technology","PLTR":"Technology","GTLB":"Technology","PATH":"Technology",
    "SMAR":"Technology","APPN":"Technology","AI":"Technology",
    # Semiconductors (Technology sub)
    "SOXL":"Technology","SOXS":"Technology","SMH":"Technology","SOXX":"Technology",
    "ASML":"Technology","TSM":"Technology","ARM":"Technology","MCHP":"Technology",
    "ADI":"Technology","NXPI":"Technology","ON":"Technology","STM":"Technology",
    "MPWR":"Technology","WOLF":"Technology","SWKS":"Technology","QRVO":"Technology",
    # Communication
    "GOOGL":"Communication","GOOG":"Communication","META":"Communication",
    "NFLX":"Communication","DIS":"Communication","CMCSA":"Communication",
    "VZ":"Communication","T":"Communication","TMUS":"Communication",
    "CHTR":"Communication","PARA":"Communication","WBD":"Communication",
    "FOXA":"Communication","FOX":"Communication","ATVI":"Communication",
    "EA":"Communication","TTWO":"Communication","RIDA":"Communication",
    "SPOT":"Communication","SNAP":"Communication","PINS":"Communication",
    "HOOD":"Communication","RDDT":"Communication",
    # ConsumerDiscretionary
    "AMZN":"ConsumerDiscretionary","TSLA":"ConsumerDiscretionary",
    "HD":"ConsumerDiscretionary","LOW":"ConsumerDiscretionary",
    "MCD":"ConsumerDiscretionary","SBUX":"ConsumerDiscretionary",
    "NKE":"ConsumerDiscretionary","TGT":"ConsumerDiscretionary",
    "BKNG":"ConsumerDiscretionary","MAR":"ConsumerDiscretionary",
    "HLT":"ConsumerDiscretionary","ABNB":"ConsumerDiscretionary",
    "LVS":"ConsumerDiscretionary","MGM":"ConsumerDiscretionary",
    "WYNN":"ConsumerDiscretionary","RCL":"ConsumerDiscretionary",
    "CCL":"ConsumerDiscretionary","NCLH":"ConsumerDiscretionary",
    "GM":"ConsumerDiscretionary","F":"ConsumerDiscretionary",
    "RIVN":"ConsumerDiscretionary","LCID":"ConsumerDiscretionary",
    "UBER":"ConsumerDiscretionary","LYFT":"ConsumerDiscretionary",
    "DKNG":"ConsumerDiscretionary","PENN":"ConsumerDiscretionary",
    "ETSY":"ConsumerDiscretionary","EBAY":"ConsumerDiscretionary",
    "W":"ConsumerDiscretionary","CPRI":"ConsumerDiscretionary",
    "RL":"ConsumerDiscretionary","PVH":"ConsumerDiscretionary",
    "TSLL":"ConsumerDiscretionary","TSLS":"ConsumerDiscretionary",
    # ConsumerStaples
    "WMT":"ConsumerStaples","COST":"ConsumerStaples","PG":"ConsumerStaples",
    "KO":"ConsumerStaples","PEP":"ConsumerStaples","PM":"ConsumerStaples",
    "MO":"ConsumerStaples","MDLZ":"ConsumerStaples","GIS":"ConsumerStaples",
    "K":"ConsumerStaples","CPB":"ConsumerStaples","SJM":"ConsumerStaples",
    "HSY":"ConsumerStaples","MKC":"ConsumerStaples","CAG":"ConsumerStaples",
    "KHC":"ConsumerStaples","KDP":"ConsumerStaples","STZ":"ConsumerStaples",
    "BF.B":"ConsumerStaples","TAP":"ConsumerStaples","COTY":"ConsumerStaples",
    "EL":"ConsumerStaples","ULTA":"ConsumerStaples","CLX":"ConsumerStaples",
    "CHD":"ConsumerStaples","CL":"ConsumerStaples",
    # Financials
    "JPM":"Financials","BAC":"Financials","WFC":"Financials","GS":"Financials",
    "MS":"Financials","C":"Financials","USB":"Financials","PNC":"Financials",
    "TFC":"Financials","COF":"Financials","AXP":"Financials","V":"Financials",
    "MA":"Financials","PYPL":"Financials","SQ":"Financials","AFRM":"Financials",
    "SOFI":"Financials","NU":"Financials","UPST":"Financials","LC":"Financials",
    "BX":"Financials","KKR":"Financials","APO":"Financials","ARES":"Financials",
    "CG":"Financials","OWL":"Financials","TPVG":"Financials","FIG":"Financials",
    "BLK":"Financials","IVZ":"Financials","SCHW":"Financials","IBKR":"Financials",
    "RJF":"Financials","SF":"Financials","EVR":"Financials","LAZ":"Financials",
    "MET":"Financials","PRU":"Financials","AFL":"Financials","ALL":"Financials",
    "PGR":"Financials","TRV":"Financials","HIG":"Financials","CB":"Financials",
    "AIG":"Financials","L":"Financials","RE":"Financials","RNR":"Financials",
    "KRE":"Financials","XLF":"Financials","FAS":"Financials","FAZ":"Financials",
    # Healthcare
    "LLY":"Healthcare","UNH":"Healthcare","JNJ":"Healthcare","ABBV":"Healthcare",
    "MRK":"Healthcare","PFE":"Healthcare","TMO":"Healthcare","ABT":"Healthcare",
    "DHR":"Healthcare","BMY":"Healthcare","AMGN":"Healthcare","GILD":"Healthcare",
    "BIIB":"Healthcare","REGN":"Healthcare","VRTX":"Healthcare","MRNA":"Healthcare",
    "BNTX":"Healthcare","ILMN":"Healthcare","IQV":"Healthcare","CRL":"Healthcare",
    "ISRG":"Healthcare","MDT":"Healthcare","EW":"Healthcare","BSX":"Healthcare",
    "SYK":"Healthcare","ZBH":"Healthcare","HUM":"Healthcare","CVS":"Healthcare",
    "CI":"Healthcare","CNC":"Healthcare","MOH":"Healthcare","HCA":"Healthcare",
    "THC":"Healthcare","DGX":"Healthcare","LH":"Healthcare","IQVIA":"Healthcare",
    "XBI":"Healthcare","IBB":"Healthcare","LABU":"Healthcare","LABD":"Healthcare",
    # Energy
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy","EOG":"Energy",
    "PXD":"Energy","DVN":"Energy","MPC":"Energy","PSX":"Energy","VLO":"Energy",
    "HAL":"Energy","BKR":"Energy","OXY":"Energy","HES":"Energy","APA":"Energy",
    "FANG":"Energy","MRO":"Energy","CVE":"Energy","CNQ":"Energy","SU":"Energy",
    "CTRA":"Energy","KMI":"Energy","WMB":"Energy","OKE":"Energy","LNG":"Energy",
    "XLE":"Energy","USO":"Energy","UNG":"Energy",
    # Industrials
    "CAT":"Industrials","DE":"Industrials","BA":"Industrials","RTX":"Industrials",
    "LMT":"Industrials","GE":"Industrials","HON":"Industrials","MMM":"Industrials",
    "UNP":"Industrials","CSX":"Industrials","NSC":"Industrials","FDX":"Industrials",
    "UPS":"Industrials","DAL":"Industrials","UAL":"Industrials","AAL":"Industrials",
    "LUV":"Industrials","JBLU":"Industrials","GD":"Industrials","NOC":"Industrials",
    "HII":"Industrials","L3H":"Industrials","TXT":"Industrials","HWM":"Industrials",
    "PWR":"Industrials","GNRC":"Industrials","XPO":"Industrials","SAIA":"Industrials",
    "ODFL":"Industrials","JBHT":"Industrials","URI":"Industrials","AGCO":"Industrials",
    # Materials
    "LIN":"Materials","APD":"Materials","ECL":"Materials","SHW":"Materials",
    "DD":"Materials","NEM":"Materials","FCX":"Materials","NUE":"Materials",
    "STLD":"Materials","RS":"Materials","CMC":"Materials","CLF":"Materials",
    "X":"Materials","MT":"Materials","AA":"Materials","CENX":"Materials",
    "MP":"Materials","VALE":"Materials","BHP":"Materials","RIO":"Materials",
    "GDX":"Materials","GDXJ":"Materials","SLV":"Materials","GLD":"Materials",
    "IAU":"Materials","SIVR":"Materials","PSLV":"Materials",
    # RealEstate
    "AMT":"RealEstate","PLD":"RealEstate","CCI":"RealEstate","EQIX":"RealEstate",
    "PSA":"RealEstate","EQR":"RealEstate","AVB":"RealEstate","SPG":"RealEstate",
    "O":"RealEstate","VICI":"RealEstate","WPC":"RealEstate","NNN":"RealEstate",
    "STOR":"RealEstate","GLPI":"RealEstate","MPW":"RealEstate",
    # Utilities
    "NEE":"Utilities","DUK":"Utilities","SO":"Utilities","AEP":"Utilities",
    "EXC":"Utilities","XEL":"Utilities","ED":"Utilities","D":"Utilities",
    "PCG":"Utilities","EIX":"Utilities","WEC":"Utilities","ES":"Utilities",
    # Crypto/Alternatives
    "BITO":"Crypto","IBIT":"Crypto","FBTC":"Crypto","GBTC":"Crypto",
    "IREN":"Crypto","MARA":"Crypto","RIOT":"Crypto","CLSK":"Crypto",
    "BITF":"Crypto","HUT":"Crypto","CIFR":"Crypto",
}

# Sector ETF proxy map for correlation-based fallback
SECTOR_ETF_MAP = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Healthcare", "XLI": "Industrials", "XLY": "ConsumerDiscretionary",
    "XLP": "ConsumerStaples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "RealEstate", "XLC": "Communication",
}


def get_sector(symbol: str, bars_dict: dict = None) -> str:
    """
    Return sector for a symbol.
    Primary: hardcoded SECTOR_MAP lookup (instant, no API).
    Fallback: correlation to sector ETFs using bars already in memory.
    Final fallback: 'Unknown'.
    """
    # Direct lookup
    sector = SECTOR_MAP.get(symbol)
    if sector:
        return sector

    # Correlation-based fallback — needs bars_dict with sector ETFs included
    if bars_dict is not None:
        sym_bars = bars_dict.get(symbol)
        if sym_bars is not None and len(sym_bars) >= 20:
            sym_ret = sym_bars["close"].pct_change().dropna().iloc[-20:].values
            best_corr   = -1.0
            best_sector = "Unknown"
            for etf, etf_sector in SECTOR_ETF_MAP.items():
                etf_bars = bars_dict.get(etf)
                if etf_bars is None or len(etf_bars) < 20:
                    continue
                etf_ret = etf_bars["close"].pct_change().dropna().iloc[-20:].values
                min_len = min(len(sym_ret), len(etf_ret))
                if min_len < 10:
                    continue
                try:
                    corr = float(np.corrcoef(sym_ret[-min_len:], etf_ret[-min_len:])[0, 1])
                    if corr > best_corr:
                        best_corr   = corr
                        best_sector = etf_sector
                except Exception:
                    continue
            return best_sector

    return "Unknown"


def get_portfolio_sector_exposure(
    held_symbols: list,
    positions: list,
    bars_dict: dict = None
) -> dict:
    """
    Compute sector exposure as % of total market value for all held positions.
    Returns {sector: pct_of_portfolio} dict.
    """
    total_mv = sum(
        float(p.get("qty", 0)) * float(p.get("current_price", 0))
        for p in positions
    )
    if total_mv <= 0:
        return {}

    exposure = {}
    for p in positions:
        sym    = p.get("symbol", "")
        mv     = float(p.get("qty", 0)) * float(p.get("current_price", 0))
        sector = get_sector(sym, bars_dict)
        exposure[sector] = exposure.get(sector, 0.0) + mv

    return {s: round(v / total_mv, 4) for s, v in exposure.items()}
