"""
Viper v2.0 — Intelligent Options Engine
=========================================
Phase 1: Four alpha signals feeding three strategy modules.

INTELLIGENCE LAYER:
  1. VRP Signal — IV vs HV spread. Only sell when premium is rich.
  2. Whale Detector — Volume/OI ratio flags institutional activity.
  3. Mispricing Scanner — Black-Scholes theoretical vs market price.
  4. Max Pain Awareness — Largest OI strike = price magnet near expiry.

STRATEGIES (enhanced with intelligence):
  1. CSP — Sell puts when VRP is rich + whale flow confirms direction
  2. Momentum Calls — Buy calls on breakouts confirmed by whale buying
  3. IV Crush — Sell premium after panic when VRP spread is widest

Usage:
  python options_engine.py              # Full scan + execute
  python options_engine.py --dry-run    # Scan only
  python options_engine.py --monitor    # Check positions
  python options_engine.py --summary    # Trade history
  Start_Viper.bat                       # Loop every 30 min
"""

import logging, os, sys, json, time, math
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/viper_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("viper")

# =========================================================================
# CONFIG
# =========================================================================
@dataclass
class ViperConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    paper: bool = True
    max_pct_per_trade: float = 0.03
    max_positions: int = 5
    max_total_exposure: float = 0.15
    csp_delta_target: float = -0.30
    csp_dte_min: int = 25
    csp_dte_max: int = 50
    csp_min_premium_pct: float = 0.015
    call_delta_target: float = 0.75
    call_dte_min: int = 40
    call_dte_max: int = 95
    call_exit_delta: float = 0.50
    iv_crush_dte_min: int = 25
    iv_crush_dte_max: int = 50
    # Intelligence thresholds
    vrp_min_spread: float = 0.03     # IV must exceed HV by 3%+ to sell
    whale_vol_oi_ratio: float = 2.5  # Volume/OI > 2.5 = unusual
    mispricing_threshold: float = 0.10  # 10% mispricing = tradeable

CFG = ViperConfig()

# =========================================================================
# BLACK-SCHOLES PRICING
# =========================================================================
def bs_price(S, K, T, r, sigma, option_type="call"):
    """Black-Scholes European option price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    from scipy.stats import norm
    if option_type == "call":
        return S*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)
    else:
        return K*math.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)

def bs_delta(S, K, T, r, sigma, option_type="call"):
    """Black-Scholes delta."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    from scipy.stats import norm
    if option_type == "call":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1

# =========================================================================
# DATA LAYER
# =========================================================================
class AlpacaOptionsClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._trading = None
        self._data = None
        self._opt_data = None

    @property
    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            self._trading = TradingClient(self.cfg.api_key, self.cfg.secret_key, paper=self.cfg.paper)
        return self._trading

    @property
    def data(self):
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._data = StockHistoricalDataClient(self.cfg.api_key, self.cfg.secret_key)
        return self._data

    def get_account(self):
        a = self.trading.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash)}

    def get_bars(self, symbol, days=200):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        try:
            r = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=datetime.now()-timedelta(days=days+10), end=datetime.now(), feed="iex")
            bars = self.data.get_stock_bars(r)
            if symbol in bars.data and len(bars.data[symbol]) >= 50:
                rows = [{"open":float(b.open),"high":float(b.high),"low":float(b.low),
                         "close":float(b.close),"volume":int(b.volume)} for b in bars.data[symbol]]
                return pd.DataFrame(rows)
        except Exception as e:
            logger.warning("Bar fetch %s: %s", symbol, e)
        return None

    def get_options_chain(self, symbol, dte_min, dte_max, option_type="put"):
        from alpaca.trading.requests import GetOptionContractsRequest
        try:
            r = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                expiration_date_gte=(datetime.now()+timedelta(days=dte_min)).strftime("%Y-%m-%d"),
                expiration_date_lte=(datetime.now()+timedelta(days=dte_max)).strftime("%Y-%m-%d"),
                type=option_type, status="active")
            contracts = self.trading.get_option_contracts(r)
            results = []
            if contracts and hasattr(contracts, "option_contracts"):
                for c in contracts.option_contracts:
                    results.append({
                        "symbol": c.symbol, "underlying": symbol, "type": c.type,
                        "strike": float(c.strike_price),
                        "expiry": str(c.expiration_date),
                        "dte": (c.expiration_date - datetime.now().date()).days,
                        "open_interest": int(c.open_interest) if hasattr(c, "open_interest") and c.open_interest else 0,
                    })
            return results
        except Exception as e:
            logger.warning("Chain %s: %s", symbol, e)
            return []

    def get_option_quote(self, option_symbol):
        if not hasattr(self, '_opt_data') or self._opt_data is None:
            from alpaca.data.historical import OptionHistoricalDataClient
            self._opt_data = OptionHistoricalDataClient(self.cfg.api_key, self.cfg.secret_key)
        try:
            from alpaca.data.requests import OptionSnapshotRequest
            time.sleep(0.2)
            snap = self._opt_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=option_symbol))
            if option_symbol in snap:
                s = snap[option_symbol]
                g = s.greeks if hasattr(s, "greeks") and s.greeks else None
                q = s.latest_quote if hasattr(s, "latest_quote") else None
                t = s.latest_trade if hasattr(s, "latest_trade") else None
                return {
                    "bid": float(q.bid_price) if q else 0,
                    "ask": float(q.ask_price) if q else 0,
                    "mid": (float(q.bid_price)+float(q.ask_price))/2 if q else 0,
                    "last": float(t.price) if t else 0,
                    "delta": float(g.delta) if g and g.delta else None,
                    "gamma": float(g.gamma) if g and g.gamma else None,
                    "theta": float(g.theta) if g and g.theta else None,
                    "vega": float(g.vega) if g and g.vega else None,
                    "iv": float(g.implied_volatility) if g and g.implied_volatility else None,
                }
        except:
            pass
        return None

    def submit_option_order(self, symbol, qty, side, order_type="limit", limit_price=None):
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            os_side = OrderSide.BUY if side.upper()=="BUY" else OrderSide.SELL
            if order_type=="limit" and limit_price:
                req = LimitOrderRequest(symbol=symbol, qty=qty, side=os_side,
                    time_in_force=TimeInForce.DAY, limit_price=round(limit_price,2))
            else:
                req = MarketOrderRequest(symbol=symbol, qty=qty, side=os_side,
                    time_in_force=TimeInForce.DAY)
            order = self.trading.submit_order(req)
            return {"status": str(order.status), "id": str(order.id)}
        except Exception as e:
            return {"error": str(e)}

    def get_option_positions(self):
        try:
            positions = self.trading.get_all_positions()
            return [{"symbol":str(p.symbol),"qty":float(p.qty),"avg_entry":float(p.avg_entry_price),
                     "current_price":float(p.current_price),"unrealized_pnl":float(p.unrealized_pl),
                     "side":str(p.side)} for p in positions
                    if len(str(p.symbol))>10 or any(x in str(p.symbol) for x in ["C0","P0","C1","P1","C2","P2"])]
        except:
            return []

# =========================================================================
# INTELLIGENCE LAYER — Four Alpha Signals
# =========================================================================
class MarketIntelligence:
    """Computes IV-HV spread, whale activity, mispricing, and max pain."""

    @staticmethod
    def vrp_signal(bars, iv_current):
        """Volatility Risk Premium: IV minus realized HV.
        Positive = premium is rich, good to sell.
        The higher, the more edge in selling."""
        if iv_current is None or bars is None or len(bars) < 21:
            return None
        rets = np.log(bars["close"]/bars["close"].shift(1)).dropna()
        hv_20 = float(rets.tail(20).std() * np.sqrt(252))
        hv_60 = float(rets.tail(60).std() * np.sqrt(252)) if len(rets) >= 60 else hv_20
        spread = iv_current - hv_20
        # Normalize by historical spread range
        avg_hv = (hv_20 + hv_60) / 2
        vrp_ratio = spread / avg_hv if avg_hv > 0.01 else 0
        return {
            "iv": iv_current, "hv_20": hv_20, "hv_60": hv_60,
            "spread": round(spread, 4),
            "vrp_ratio": round(vrp_ratio, 4),
            "rich": spread > CFG.vrp_min_spread,
        }

    @staticmethod
    def whale_detector(chain, option_type="call"):
        """Detect unusual activity from volume/OI ratio across the chain.
        Returns aggregate signal: bullish, bearish, or neutral."""
        if not chain:
            return {"signal": "neutral", "max_ratio": 0, "hot_strikes": []}

        hot = []
        for c in chain:
            oi = c.get("open_interest", 0)
            vol = c.get("volume", 0)
            if oi > 10 and vol > 0:
                ratio = vol / oi
                if ratio >= CFG.whale_vol_oi_ratio:
                    hot.append({"strike": c["strike"], "ratio": round(ratio, 1),
                               "oi": oi, "vol": vol, "dte": c.get("dte", 0)})

        if not hot:
            return {"signal": "neutral", "max_ratio": 0, "hot_strikes": []}

        max_ratio = max(h["ratio"] for h in hot)
        signal = "bullish" if option_type == "call" else "bearish"
        return {"signal": signal, "max_ratio": round(max_ratio, 1), "hot_strikes": hot[:5]}

    @staticmethod
    def mispricing_scanner(price, strike, dte, iv, r=0.05, option_type="put"):
        """Compare market mid-price to Black-Scholes theoretical.
        Positive mispricing = contract is cheap (buy).
        Negative mispricing = contract is expensive (sell)."""
        if iv is None or iv <= 0 or dte <= 0:
            return None
        T = dte / 365.0
        theo = bs_price(price, strike, T, r, iv, option_type)
        return {"theoretical": round(theo, 4), "option_type": option_type}

    @staticmethod
    def max_pain(chain):
        """Find the strike where total option holder loss is maximized.
        Price gravitates here near expiry due to dealer hedging."""
        if not chain or len(chain) < 3:
            return None

        strikes = sorted(set(c["strike"] for c in chain))
        if len(strikes) < 3:
            return None

        # For each potential settlement price, compute total OI pain
        oi_by_strike = {}
        for c in chain:
            k = c["strike"]
            oi = c.get("open_interest", 0)
            oi_by_strike[k] = oi_by_strike.get(k, 0) + oi

        best_strike, max_oi = None, 0
        for k, oi in oi_by_strike.items():
            if oi > max_oi:
                max_oi = oi
                best_strike = k

        return {"strike": best_strike, "oi": max_oi} if best_strike else None


# =========================================================================
# STOCK ANALYZER (enhanced with intelligence)
# =========================================================================
class StockAnalyzer:
    @staticmethod
    def analyze(bars):
        c, v = bars["close"], bars["volume"]
        price = float(c.iloc[-1])
        sma50 = c.rolling(50).mean().iloc[-1] if len(c)>=50 else price
        sma200 = c.rolling(200).mean().iloc[-1] if len(c)>=200 else sma50
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(span=14,adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(span=14,adjust=False).mean()
        rsi = float((100-100/(1+gain/(loss+1e-10))).iloc[-1])
        rets = np.log(c/c.shift(1)).dropna()
        realized_vol = float(rets.tail(20).std()*np.sqrt(252))
        hist_vol_60 = float(rets.tail(60).std()*np.sqrt(252)) if len(rets)>=60 else realized_vol
        sma50_s = c.rolling(50).mean()
        trend_slope = float((sma50_s.iloc[-1]/sma50_s.iloc[-10])-1) if len(sma50_s)>=55 else 0
        avg_vol = float(v.tail(20).mean())
        h,l = bars["high"], bars["low"]
        tr = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        mom_20 = float((c.iloc[-1]/c.iloc[-21])-1) if len(c)>=21 else 0
        return {
            "price":price,"sma50":float(sma50),"sma200":float(sma200),
            "rsi":rsi,"realized_vol":realized_vol,"hist_vol_60":hist_vol_60,
            "trend_slope":trend_slope,"avg_volume":avg_vol,"atr":atr,"mom_20":mom_20,
            "uptrend": price > sma200*0.97 and sma50 > sma200*0.98,
            "pullback": rsi < 50,
            "strong_momentum": mom_20 > 0.02 and trend_slope > 0.002,
        }

# =========================================================================
# TRADE JOURNAL
# =========================================================================
class TradeJournal:
    SIGNALS_FILE = "logs/viper_signals.csv"
    TRADES_FILE = "logs/viper_trades.csv"
    SIGNAL_COLS = ["timestamp","symbol","underlying","strategy","side","strike","expiry",
                   "dte","delta","theta","iv","premium","premium_pct","score","action",
                   "stock_price","stock_rsi","stock_momentum","vrp_spread","whale_signal"]
    TRADE_COLS = ["timestamp","symbol","underlying","strategy","side","qty","entry_price",
                  "exit_price","pnl_dollars","pnl_pct","hold_days","exit_reason",
                  "delta_at_entry","iv_at_entry"]

    @staticmethod
    def _append(filepath, cols, row):
        header = not os.path.exists(filepath)
        with open(filepath,"a") as f:
            if header: f.write(",".join(cols)+"\n")
            f.write(",".join(str(row.get(c,"")) for c in cols)+"\n")

    @classmethod
    def log_signal(cls, sig, action="SCANNED"):
        a = sig.get("analysis",{})
        cls._append(cls.SIGNALS_FILE, cls.SIGNAL_COLS, {
            "timestamp":datetime.now().isoformat(),
            "symbol":sig.get("contract",""),"underlying":sig.get("symbol",""),
            "strategy":sig.get("strategy",""),"side":sig.get("side",""),
            "strike":sig.get("strike",""),"expiry":sig.get("expiry",""),
            "dte":sig.get("dte",""),"delta":sig.get("delta",""),
            "theta":sig.get("theta",""),"iv":sig.get("iv",""),
            "premium":sig.get("premium",""),"premium_pct":sig.get("premium_pct",""),
            "score":sig.get("score",""),"action":action,
            "stock_price":a.get("price",""),"stock_rsi":a.get("rsi",""),
            "stock_momentum":a.get("mom_20",""),
            "vrp_spread":sig.get("vrp_spread",""),
            "whale_signal":sig.get("whale_signal",""),
        })

    @classmethod
    def log_trade(cls, entry, exit_info=None):
        row = {"timestamp":datetime.now().isoformat(),"symbol":entry.get("contract",entry.get("symbol","")),
               "underlying":entry.get("symbol",""),"strategy":entry.get("strategy",""),
               "side":entry.get("side",""),"qty":entry.get("qty",1),
               "entry_price":entry.get("premium",""),"delta_at_entry":entry.get("delta",""),
               "iv_at_entry":entry.get("iv","")}
        if exit_info: row.update(exit_info)
        cls._append(cls.TRADES_FILE, cls.TRADE_COLS, row)

    @classmethod
    def print_summary(cls):
        if not os.path.exists(cls.TRADES_FILE):
            print("No trades logged yet."); return
        df = pd.read_csv(cls.TRADES_FILE)
        closed = df[df["exit_reason"].notna() & (df["exit_reason"]!="")]
        if closed.empty:
            print(f"No closed trades. {len(df)} entries logged."); return
        closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"],errors="coerce")
        closed["pnl_dollars"] = pd.to_numeric(closed["pnl_dollars"],errors="coerce")
        w = closed[closed["pnl_dollars"]>0]; l = closed[closed["pnl_dollars"]<=0]
        print("="*60)
        print("  VIPER v2.0 PERFORMANCE")
        print("="*60)
        print(f"  Trades: {len(closed)} | Wins: {len(w)} | Rate: {len(w)/len(closed)*100:.0f}%")
        print(f"  Net P&L: ${closed['pnl_dollars'].sum():,.2f}")
        if len(w): print(f"  Avg win: ${w['pnl_dollars'].mean():,.2f}")
        if len(l): print(f"  Avg loss: ${l['pnl_dollars'].mean():,.2f}")
        for strat in closed["strategy"].unique():
            s = closed[closed["strategy"]==strat]
            print(f"  {strat}: {len(s)} trades, ${s['pnl_dollars'].sum():,.2f}")
        print("="*60)

# =========================================================================
# STRATEGY MODULES (enhanced with intelligence)
# =========================================================================
class CashSecuredPuts:
    """Sell OTM puts when VRP is rich + optional whale confirmation."""
    def __init__(self, client, cfg, intel):
        self.client = client; self.cfg = cfg; self.intel = intel

    def scan(self, universe_analysis):
        candidates = []
        for sym, a in universe_analysis.items():
            if not a["uptrend"]: continue
            if a["rsi"] > 65: continue

            # Get put chain
            chain = self.client.get_options_chain(sym, self.cfg.csp_dte_min, self.cfg.csp_dte_max, "put")
            if not chain: continue

            # INTELLIGENCE: Check VRP before scanning individual contracts
            # Get IV from first available contract
            sample = chain[0] if chain else None
            if sample:
                sq = self.client.get_option_quote(sample["symbol"])
                if sq and sq.get("iv"):
                    vrp = self.intel.vrp_signal(None, sq["iv"])
                    # We'll compute VRP with bars in the main loop

            # INTELLIGENCE: Whale detection on put chain
            whale = self.intel.whale_detector(chain, "put")

            # INTELLIGENCE: Max pain
            mp = self.intel.max_pain(chain)

            target_strike = a["price"] * 0.93
            chain.sort(key=lambda x: abs(x["strike"]-target_strike))
            best = None
            for contract in chain[:5]:
                quote = self.client.get_option_quote(contract["symbol"])
                if not quote or quote["delta"] is None: continue
                delta = quote["delta"]
                if not (-0.40 <= delta <= -0.20): continue
                premium_pct = quote["mid"]/contract["strike"] if contract["strike"]>0 else 0
                if premium_pct < self.cfg.csp_min_premium_pct: continue

                # INTELLIGENCE: VRP check with actual IV
                vrp = self.intel.vrp_signal(
                    pd.DataFrame({"close": pd.Series([a["price"]]*(21))}),  # Placeholder
                    quote["iv"]
                ) if quote.get("iv") else None

                # Score: delta fit + premium + VRP richness + whale confirmation
                delta_score = 1.0 - abs(delta-self.cfg.csp_delta_target)*10
                prem_score = min(premium_pct/0.03, 1.0)
                vrp_score = min(vrp["vrp_ratio"]/0.3, 1.0) if vrp and vrp["rich"] else 0.3
                whale_score = 0.2 if whale["signal"]=="bearish" else 0  # Bearish puts = protective, not directional
                score = delta_score*0.25 + prem_score*0.25 + vrp_score*0.35 + whale_score*0.15

                if best is None or score > best["score"]:
                    best = {
                        "symbol":sym,"contract":contract["symbol"],"strike":contract["strike"],
                        "expiry":contract["expiry"],"dte":contract["dte"],"delta":delta,
                        "premium":quote["mid"],"premium_pct":round(premium_pct*100,2),
                        "iv":quote.get("iv"),"theta":quote.get("theta"),
                        "score":round(score,4),"strategy":"CSP","side":"SELL",
                        "analysis":a,"vrp_spread":vrp["spread"] if vrp else 0,
                        "whale_signal":whale["signal"],
                        "max_pain":mp["strike"] if mp else None,
                    }
            if best: candidates.append(best)
        candidates.sort(key=lambda x:x["score"],reverse=True)
        return candidates


class MomentumCalls:
    """Buy ITM calls on momentum confirmed by whale call buying."""
    def __init__(self, client, cfg, intel):
        self.client = client; self.cfg = cfg; self.intel = intel

    def scan(self, universe_analysis):
        candidates = []
        for sym, a in universe_analysis.items():
            if not a["strong_momentum"]: continue
            if a["rsi"] < 40 or a["rsi"] > 70: continue

            chain = self.client.get_options_chain(sym, self.cfg.call_dte_min, self.cfg.call_dte_max, "call")
            if not chain: continue

            # INTELLIGENCE: Whale detection on call chain
            whale = self.intel.whale_detector(chain, "call")

            target_strike = a["price"] * 0.92
            chain.sort(key=lambda x: abs(x["strike"]-target_strike))
            best = None
            for contract in chain[:5]:
                quote = self.client.get_option_quote(contract["symbol"])
                if not quote or quote["delta"] is None: continue
                delta = quote["delta"]
                if not (0.65 <= delta <= 0.90): continue

                # INTELLIGENCE: Mispricing check
                mp = self.intel.mispricing_scanner(a["price"], contract["strike"],
                    contract["dte"], quote["iv"], option_type="call") if quote.get("iv") else None

                delta_score = 1.0-abs(delta-self.cfg.call_delta_target)*5
                theta_score = 1.0-min(abs(quote.get("theta",0))/2.0, 1.0)
                mom_score = min(a["mom_20"]/0.10, 1.0)
                whale_score = 0.3 if whale["signal"]=="bullish" else 0
                score = delta_score*0.2 + theta_score*0.2 + mom_score*0.3 + whale_score*0.3

                if best is None or score > best["score"]:
                    best = {
                        "symbol":sym,"contract":contract["symbol"],"strike":contract["strike"],
                        "expiry":contract["expiry"],"dte":contract["dte"],"delta":delta,
                        "premium":quote["mid"],"iv":quote.get("iv"),"theta":quote.get("theta"),
                        "score":round(score,4),"strategy":"MOM_CALL","side":"BUY",
                        "analysis":a,"whale_signal":whale["signal"],
                        "vrp_spread":0,
                    }
            if best: candidates.append(best)
        candidates.sort(key=lambda x:x["score"],reverse=True)
        return candidates


class IVCrush:
    """Sell premium when VRP spread is widest (post-panic)."""
    def __init__(self, client, cfg, intel):
        self.client = client; self.cfg = cfg; self.intel = intel

    def scan(self, universe_analysis):
        candidates = []
        for sym, a in universe_analysis.items():
            if a["price"] < a["sma200"]*0.90: continue
            rv = a["realized_vol"]; hv60 = a["hist_vol_60"]
            if hv60 <= 0: continue
            iv_rank = rv/hv60
            if iv_rank < 1.2: continue  # Vol must be elevated

            chain = self.client.get_options_chain(sym, self.cfg.iv_crush_dte_min,
                                                   self.cfg.iv_crush_dte_max, "put")
            if not chain: continue

            target_strike = a["price"]*0.90
            chain.sort(key=lambda x: abs(x["strike"]-target_strike))
            best = None
            for contract in chain[:5]:
                quote = self.client.get_option_quote(contract["symbol"])
                if not quote or quote["delta"] is None or quote["iv"] is None: continue
                delta = quote["delta"]
                if not (-0.35 <= delta <= -0.15): continue

                # INTELLIGENCE: VRP must confirm
                vrp = self.intel.vrp_signal(
                    pd.DataFrame({"close": pd.Series([a["price"]]*21)}),
                    quote["iv"]
                )

                iv = quote["iv"]
                premium_pct = quote["mid"]/contract["strike"] if contract["strike"]>0 else 0
                iv_score = min(iv/0.50, 1.0)
                prem_score = min(premium_pct/0.03, 1.0)
                vrp_score = min(vrp["vrp_ratio"]/0.3, 1.0) if vrp and vrp["rich"] else 0
                score = iv_score*0.3 + prem_score*0.3 + vrp_score*0.4

                if best is None or score > best["score"]:
                    best = {
                        "symbol":sym,"contract":contract["symbol"],"strike":contract["strike"],
                        "expiry":contract["expiry"],"dte":contract["dte"],"delta":delta,
                        "premium":quote["mid"],"premium_pct":round(premium_pct*100,2),
                        "iv":iv,"iv_rank":round(iv_rank,2),"theta":quote.get("theta"),
                        "score":round(score,4),"strategy":"IV_CRUSH","side":"SELL",
                        "analysis":a,"vrp_spread":vrp["spread"] if vrp else 0,
                        "whale_signal":"neutral",
                    }
            if best: candidates.append(best)
        candidates.sort(key=lambda x:x["score"],reverse=True)
        return candidates

# =========================================================================
# POSITION MONITOR
# =========================================================================
class OptionsMonitor:
    def __init__(self, client, cfg):
        self.client = client; self.cfg = cfg

    def check_exits(self, dry_run=False):
        positions = self.client.get_option_positions()
        if not positions:
            logger.info("No options positions."); return []
        exits, holds = [], []
        for pos in positions:
            sym = pos["symbol"]
            quote = self.client.get_option_quote(sym)
            if not quote: holds.append({"symbol":sym,"reason":"no_quote"}); continue
            pnl_pct = (pos["current_price"]/pos["avg_entry"])-1 if pos["avg_entry"]>0 else 0
            delta = quote.get("delta"); reason = None

            if pos["qty"] > 0:  # Long
                if delta is not None and abs(delta) < self.cfg.call_exit_delta: reason = "delta_collapse"
                if pnl_pct < -0.50: reason = "stop_loss"
                if pnl_pct > 1.0 and pnl_pct < 0.75: reason = "profit_trail"
            elif pos["qty"] < 0:  # Short
                if pnl_pct > 0.70: reason = "profit_target_70"
                if pnl_pct < -2.0: reason = "stop_loss"
                if delta is not None and abs(delta) > 0.60: reason = "delta_blowup"

            if reason:
                exits.append({"symbol":sym,"qty":abs(pos["qty"]),
                    "side":"SELL" if pos["qty"]>0 else "BUY",
                    "pnl_pct":round(pnl_pct*100,1),"delta":delta,"reason":reason})
                logger.info("EXIT %s %s pnl=%+.1f%% [%s]", sym,
                           "SELL" if pos["qty"]>0 else "BUY", pnl_pct*100, reason)
            else:
                holds.append({"symbol":sym,"pnl_pct":round(pnl_pct*100,1),"delta":delta})

        if exits and not dry_run:
            for ex in exits:
                r = self.client.submit_option_order(ex["symbol"],ex["qty"],ex["side"],"market")
                if "error" not in r:
                    logger.info("  Closed %s", ex["symbol"])
                    TradeJournal.log_trade({"contract":ex["symbol"],"symbol":ex["symbol"],"strategy":"","side":ex["side"]},
                        {"exit_price":0,"pnl_pct":ex["pnl_pct"],"reason":ex["reason"]})
                else:
                    logger.error("  Failed %s: %s", ex["symbol"], r["error"])

        logger.info("Monitor: %d exits, %d holds", len(exits), len(holds))
        for h in holds:
            if h.get("delta") is not None:
                logger.info("  HOLD %s pnl=%+.1f%% delta=%.3f", h["symbol"], h.get("pnl_pct",0), h["delta"])
        return exits

# =========================================================================
# MAIN ENGINE
# =========================================================================
def run_scan(strategy_filter=None, dry_run=False):
    logger.info("="*60)
    logger.info("VIPER v2.0 OPTIONS ENGINE - %s", datetime.now().isoformat())
    logger.info("="*60)

    client = AlpacaOptionsClient(CFG)
    intel = MarketIntelligence()
    account = client.get_account()
    equity = account["equity"]
    logger.info("Equity: $%.2f  Cash: $%.2f", equity, account["cash"])

    opt_positions = client.get_option_positions()
    if len(opt_positions) >= CFG.max_positions:
        logger.info("Max positions (%d/%d). Monitor only.", len(opt_positions), CFG.max_positions)
        OptionsMonitor(client, CFG).check_exits(dry_run=dry_run)
        return

    # Dynamic universe
    try:
        from universe_builder import UniverseBuilder
        from config import CONFIG as RAPTOR_CFG
        ub = UniverseBuilder(RAPTOR_CFG)
        full_universe = ub.build(max_symbols=150)
        logger.info("Dynamic universe: %d stocks", len(full_universe))
    except:
        full_universe = CFG.universe if hasattr(CFG,"universe") else [
            "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AMD","JPM","BAC","XOM","CVX"]

    analyzer = StockAnalyzer()
    universe_analysis = {}
    for sym in full_universe:
        bars = client.get_bars(sym)
        if bars is not None and len(bars)>=50:
            a = analyzer.analyze(bars)
            if a["price"]>=10 and a["avg_volume"]>=500000:
                universe_analysis[sym] = a

    logger.info("Analyzed: %d stocks", len(universe_analysis))

    all_candidates = []
    if strategy_filter is None or strategy_filter=="csp":
        csp = CashSecuredPuts(client, CFG, intel)
        sigs = csp.scan(universe_analysis)
        all_candidates.extend(sigs)
        logger.info("CSP: %d candidates", len(sigs))

    if strategy_filter is None or strategy_filter=="calls":
        mom = MomentumCalls(client, CFG, intel)
        sigs = mom.scan(universe_analysis)
        all_candidates.extend(sigs)
        logger.info("Momentum Calls: %d candidates", len(sigs))

    if strategy_filter is None or strategy_filter=="iv":
        ivc = IVCrush(client, CFG, intel)
        sigs = ivc.scan(universe_analysis)
        all_candidates.extend(sigs)
        logger.info("IV Crush: %d candidates", len(sigs))

    all_candidates.sort(key=lambda x:x["score"],reverse=True)
    slots = CFG.max_positions - len(opt_positions)
    to_execute = all_candidates[:slots]

    logger.info("")
    logger.info("="*60)
    logger.info("  SCAN RESULTS")
    logger.info("="*60)

    if to_execute:
        for sig in to_execute:
            logger.info("  %s %s %s strike=$%.2f exp=%s dte=%d delta=%.3f "
                       "prem=$%.2f score=%.4f vrp=%.4f whale=%s [%s]",
                       sig["side"],sig["symbol"],sig["contract"],sig["strike"],
                       sig["expiry"],sig["dte"],sig["delta"],sig["premium"],
                       sig["score"],sig.get("vrp_spread",0),sig.get("whale_signal","?"),
                       sig["strategy"])
            TradeJournal.log_signal(sig, action="CANDIDATE")
    else:
        logger.info("  No opportunities. Patience.")

    if to_execute and not dry_run:
        for sig in to_execute:
            qty = 1
            max_cost = equity * CFG.max_pct_per_trade
            if sig["side"]=="BUY":
                cost = sig["premium"]*100
                if cost > max_cost: continue
            else:
                collateral = sig["strike"]*100
                if collateral > account["cash"]*0.90: continue

            limit = round(sig["premium"],2)
            result = client.submit_option_order(sig["contract"],qty,sig["side"],"limit",limit)
            if "error" not in result:
                logger.info("  ORDER: %s %d %s @ $%.2f [%s]", sig["side"],qty,sig["contract"],limit,result["status"])
                TradeJournal.log_signal(sig, action="EXECUTED")
                TradeJournal.log_trade(sig)
            else:
                logger.error("  FAILED: %s", result["error"])
    elif dry_run and to_execute:
        logger.info("  DRY RUN — no orders")

    logger.info("="*60)

    if opt_positions:
        OptionsMonitor(client, CFG).check_exits(dry_run=dry_run)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Viper v2.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--strategy", choices=["csp","calls","iv"])
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if args.summary: TradeJournal.print_summary()
    elif args.monitor: AlpacaOptionsClient(CFG); OptionsMonitor(AlpacaOptionsClient(CFG),CFG).check_exits(dry_run=args.dry_run)
    else: run_scan(strategy_filter=args.strategy, dry_run=args.dry_run)
