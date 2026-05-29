"""
Raptor v5.4 — Merged Adaptive Engine (16 factors)
The 208% backtest engine. Do not modify factors.
"""
import json, logging, os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from config import RaptorConfig

logger = logging.getLogger("raptor.signals")
MIN_BARS_REQUIRED = 80

@dataclass
class Signal:
    symbol: str; side: str; composite_score: float; composite_percentile: float
    t_statistic: float; factor_scores: Dict[str,float]; factor_contributions: Dict[str,float]
    factors_positive: int; regime: str; sentiment_score: float; atr: float
    entry_price: float; stop_price: float; take_profit: float; kelly_fraction: float
    hold_target_days: int; leverage_qualified: bool; confirmation_type: str; timestamp: str

class Factors:
    @staticmethod
    def rsi_mr(c, period=5):
        d=c.diff(); g=d.clip(lower=0).ewm(span=period,adjust=False).mean()
        l=(-d.clip(upper=0)).ewm(span=period,adjust=False).mean()
        return float((50-(100-100/(1+g/(l+1e-10))).iloc[-1])/50)
    @staticmethod
    def bollinger_z(c, period=20):
        m,s=c.rolling(period).mean().iloc[-1],c.rolling(period).std().iloc[-1]
        return float(-(c.iloc[-1]-m)/s) if s>1e-10 else 0.0
    @staticmethod
    def crowd_panic(df):
        c,v=df["close"],df["volume"]; av=v.iloc[-21:-1].mean()
        if av<=0: return 0.0
        p=0.0
        for i in [-1,-2,-3]:
            if len(c)<abs(i)+1: continue
            r=c.iloc[i]/c.iloc[i-1]-1
            if r<0: p+=(v.iloc[i]/av)*abs(r)
        return float(p)
    @staticmethod
    def ma_distance(c):
        e8=c.ewm(span=8,adjust=False).mean().iloc[-1]
        e21=c.ewm(span=21,adjust=False).mean().iloc[-1]
        e50=c.ewm(span=50,adjust=False).mean().iloc[-1]
        a=(e8+e21+e50)/3
        return float(-(c.iloc[-1]-a)/a) if a!=0 else 0.0
    @staticmethod
    def hurst(c, min_window=8, max_window=40):
        """
        DFA (Detrended Fluctuation Analysis) Hurst exponent.
        Kantelhardt et al. 2002 — more robust than R/S for financial series:
          - R/S is biased by short-range autocorrelation and non-stationarity
          - DFA detrends within each window before measuring fluctuation
          - Returns 0.5 - H so sign convention matches R/S version:
              positive  = mean-reverting (H < 0.5)
              ~0        = random walk   (H ≈ 0.5)
              negative  = trending      (H > 0.5)
        Minimum 60 bars required (supports max_window=40 with 1.5x safety margin).
        """
        r = np.log(c / c.shift(1)).dropna().values
        n = len(r)
        if n < 60: return np.nan
        x = np.cumsum(r - r.mean())  # profile (integrated, demeaned series)
        # Window sizes: log-spaced integers between min_window and max_window
        windows = np.unique(np.round(
            np.exp(np.linspace(np.log(min_window), np.log(min(max_window, n // 4)), 12))
        ).astype(int))
        windows = windows[windows >= min_window]
        if len(windows) < 4: return np.nan
        pts = []
        for w in windows:
            segs = n // w
            if segs < 2: continue
            F2 = []
            for i in range(segs):
                seg = x[i * w:(i + 1) * w]
                # Linear detrending within window (DFA-1)
                t = np.arange(w)
                coef = np.polyfit(t, seg, 1)
                trend = np.polyval(coef, t)
                F2.append(np.mean((seg - trend) ** 2))
            F = np.sqrt(np.mean(F2))
            if F > 1e-14:
                pts.append((np.log(w), np.log(F)))
        if len(pts) < 4: return np.nan
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        H = float(np.polyfit(xs, ys, 1)[0])   # DFA exponent
        return float(0.5 - H)                  # same sign convention as prior R/S
    @staticmethod
    def ma_stack(c):
        e8=c.ewm(span=8,adjust=False).mean()
        e21=c.ewm(span=21,adjust=False).mean()
        e50=c.ewm(span=50,adjust=False).mean()
        order=float((e8.iloc[-1]>e21.iloc[-1])+(e21.iloc[-1]>e50.iloc[-1])-1)
        s=np.clip(sum((e.iloc[-1]/e.iloc[-5]-1) for e in [e8,e21,e50])/3*50,-0.4,0.4)
        return float(order*0.6+s)
    @staticmethod
    def macd_accel(c, fast=12, slow=26, sig=9):
        ef=c.ewm(span=fast,adjust=False).mean(); es=c.ewm(span=slow,adjust=False).mean()
        h=ef-es-(ef-es).ewm(span=sig,adjust=False).mean()
        return float(np.polyfit(np.arange(5),h.iloc[-5:].values,1)[0]/c.iloc[-1])
    @staticmethod
    def adx_dir(df, period=14):
        h,l,c=df["high"],df["low"],df["close"]
        pdm=h.diff().clip(lower=0); mdm=(-l.diff()).clip(lower=0)
        pdm[pdm<mdm]=0.0; mdm[mdm<pdm]=0.0
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        a=tr.ewm(span=period,adjust=False).mean()
        pdi=100*pdm.ewm(span=period,adjust=False).mean()/a
        mdi=100*mdm.ewm(span=period,adjust=False).mean()/a
        dx=100*(pdi-mdi).abs()/(pdi+mdi+1e-10)
        adx=dx.ewm(span=period,adjust=False).mean()
        return float(adx.iloc[-1]*(1.0 if pdi.iloc[-1]>mdi.iloc[-1] else -1.0))
    @staticmethod
    def adx_raw(df, period=14):
        h,l,c=df["high"],df["low"],df["close"]
        pdm=h.diff().clip(lower=0); mdm=(-l.diff()).clip(lower=0)
        pdm[pdm<mdm]=0.0; mdm[mdm<pdm]=0.0
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        a=tr.ewm(span=period,adjust=False).mean()
        pdi=100*pdm.ewm(span=period,adjust=False).mean()/a
        mdi=100*mdm.ewm(span=period,adjust=False).mean()/a
        dx=100*(pdi-mdi).abs()/(pdi+mdi+1e-10)
        return float(dx.ewm(span=period,adjust=False).mean().iloc[-1])
    @staticmethod
    def price_cloud(c):
        e8=c.ewm(span=8,adjust=False).mean().iloc[-1]
        e50=c.ewm(span=50,adjust=False).mean().iloc[-1]
        w=abs(e8-e50)
        return float((c.iloc[-1]-(e8+e50)/2)/w) if w>1e-10 else 0.0
    @staticmethod
    def vol_ratio(v):
        a=v.iloc[-21:-1].mean()
        return float(np.log(v.iloc[-1]/a)) if a>0 else np.nan
    @staticmethod
    def obv_r2(df, lb=10):
        obv=(np.sign(df["close"].diff())*df["volume"]).cumsum()
        y=obv.iloc[-lb:].values; ys=(y-y.mean())/(y.std()+1e-10)
        s,_,r,_,_=scipy_stats.linregress(np.arange(lb,dtype=float),ys)
        return float(s*r**2)
    @staticmethod
    def accum_dist(df, lb=10):
        # Uses slope × R² — consistent with obv_r2. Previously used abs(r) which
        # underweights clean trends relative to obv_r2. R² is the correct quality
        # weight: unsigned, symmetrically penalises noisy fits. (H-list fix)
        clv=((df["close"]-df["low"])-(df["high"]-df["close"]))/(df["high"]-df["low"]+1e-10)
        ad=(clv*df["volume"]).cumsum()
        y=ad.iloc[-lb:].values; ys=(y-y.mean())/(y.std()+1e-10)
        s,_,r,_,_=scipy_stats.linregress(np.arange(lb,dtype=float),ys)
        return float(s*r**2)
    @staticmethod
    def atr_pctile(df, atr_p=14, lb=60):
        h,l,c=df["high"],df["low"],df["close"]
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        a=tr.rolling(atr_p).mean().dropna()
        if len(a)<lb: return np.nan
        return float(-(scipy_stats.percentileofscore(a.iloc[-lb:].values,a.iloc[-1])/100-0.5)*2)
    @staticmethod
    def bb_squeeze(c, period=20, lb=60):
        bw=(4*c.rolling(period).std()/c.rolling(period).mean()).dropna()
        if len(bw)<lb: return np.nan
        return float(-(scipy_stats.percentileofscore(bw.iloc[-lb:].values,bw.iloc[-1])/100-0.5)*2)
    @staticmethod
    def rel_strength(sym_c, spy_c, period=10):
        if len(spy_c)<period: return np.nan
        return float((sym_c.iloc[-1]/sym_c.iloc[-period])-(spy_c.iloc[-1]/spy_c.iloc[-period]))
    @staticmethod
    def reversal_momentum(df, lookback=3):
        c,l_col,h=df["close"],df["low"],df["high"]
        tr=pd.concat([h-l_col,(h-c.shift(1)).abs(),(l_col-c.shift(1)).abs()],axis=1).max(axis=1)
        a=tr.rolling(14).mean().iloc[-1]
        if pd.isna(a) or a<=0: return np.nan
        return float((c.iloc[-1]-l_col.iloc[-lookback:].min())/a)
    @staticmethod
    def atr(df, period=14):
        h,l,c=df["high"],df["low"],df["close"]
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])
    @staticmethod
    def check_leverage(df, spy_bars, rsi_val, bb_z):
        if spy_bars is None or len(spy_bars)<205: return False
        spy_c=spy_bars["close"]; sma200=spy_c.rolling(200).mean()
        if not(spy_c.iloc[-1]>sma200.iloc[-1] and sma200.iloc[-1]>sma200.iloc[-5]): return False
        if rsi_val>=30 or bb_z<2.0: return False
        c,h,l=df["close"],df["high"],df["low"]
        ema20=c.ewm(span=20,adjust=False).mean()
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        kl=ema20-1.5*tr.rolling(14).mean()
        if c.iloc[-1]>=kl.iloc[-1]: return False
        av=df["volume"].iloc[-21:-1].mean()
        if av<=0 or df["volume"].iloc[-1]/av<1.5: return False
        return True

class AdaptiveWeights:
    WEIGHT_FILE="adaptive_weights.json"; MIN_TRADES=30; MAX_ALPHA=0.30; RIDGE_LAMBDA=1.0
    def __init__(self, factor_names, base_dir="."):
        self.factor_names=factor_names
        self.path=os.path.join(base_dir,self.WEIGHT_FILE); self.data=self._load()
        self._ic_cache=None  # (n_trades, {fn: ic}) — reused across all symbols in one scan
    def _load(self):
        if os.path.exists(self.path):
            with open(self.path,"r") as f: return json.load(f)
        return {"trades":[],"ridge_beta":None,"n_trades":0}
    def _save(self):
        with open(self.path,"w") as f: json.dump(self.data,f,indent=2)
    def record_trade(self, zscores, ret):
        row={fn:zscores.get(fn,0.0) for fn in self.factor_names}; row["y"]=ret
        self.data["trades"].append(row); self.data["n_trades"]=len(self.data["trades"])
        self._fit(); self._save()
    def _get_ic_boost(self):
        # Spearman rank IC: correlation between factor z-score and realized return.
        # Replaces binary sign-agreement which discarded magnitude entirely.
        # A factor that calls direction right by a hair scores the same as one
        # that called a 20% move — sign-agreement cannot distinguish them.
        # Grinold & Kahn (1999): IC = rank_corr(factor_score, forward_return).
        # Decay-weighted: recent trades contribute proportionally more.
        # Reference: CRIT-3 in RAPTOR_MASTER_PLAN.md
        n=len(self.data["trades"])
        if n<20: return {}
        if self._ic_cache and self._ic_cache[0]==n: return self._ic_cache[1]
        recent=self.data["trades"][-50:]
        y_vals=[t.get("y",0.0) for t in recent]
        ic={}
        for fn in self.factor_names:
            x_vals=[t.get(fn,0.0) for t in recent]
            # Need at least 5 pairs and non-constant x to compute rank correlation
            if len(set(x_vals))<3:
                ic[fn]=0.0
                continue
            try:
                rho,_=scipy_stats.spearmanr(x_vals,y_vals)
                ic[fn]=float(rho) if not np.isnan(rho) else 0.0
            except Exception:
                ic[fn]=0.0
        self._ic_cache=(n,ic); return ic
    def _fit(self):
        t=self.data["trades"]
        if len(t)<self.MIN_TRADES: self.data["ridge_beta"]=None; return
        X=np.array([[tr.get(fn,0) for fn in self.factor_names] for tr in t])
        y=np.array([tr["y"] for tr in t]); k=len(self.factor_names)
        try: self.data["ridge_beta"]=np.linalg.solve(X.T@X+self.RIDGE_LAMBDA*np.eye(k),X.T@y).tolist()
        except: self.data["ridge_beta"]=None
    def blend_weights(self, base):
        if self.data["ridge_beta"] is None and not self.data.get("ic_weights"):
            return base
        blended = dict(base)
        n = self.data["n_trades"]
        # Layer 1: Ridge regression (existing)
        if self.data["ridge_beta"] is not None:
            b=np.abs(np.array(self.data["ridge_beta"]))
            if b.sum()>1e-10:
                norm=b/b.sum()
                ra={fn:float(norm[i]) for i,fn in enumerate(self.factor_names)}
                a=min(self.MAX_ALPHA,self.MAX_ALPHA*(n-self.MIN_TRADES)/(2*self.MIN_TRADES)); a=max(0,a)
                blended={fn:(1-a)*base[fn]+a*ra.get(fn,base[fn]) for fn in base}
        # Layer 2: Factor IC — pre-computed once per scan, not per symbol
        ic_boost=self._get_ic_boost()
        if ic_boost:
            blended={fn:blended.get(fn,0)*(1.0+ic_boost.get(fn,0)) for fn in self.factor_names}
        tot=sum(blended.values())
        return {k:v/tot for k,v in blended.items()} if tot>1e-10 else base

FACTOR_NAMES = [
    "rsi_mr","bollinger_z","crowd_panic","ma_distance","hurst",
    "ma_stack","macd_accel","adx_dir","price_cloud",
    "vol_ratio","obv_r2","accum_dist",
    "atr_pctile","bb_squeeze","rel_strength",
    "rev_momentum",
]
FACTOR_CLUSTERS = {
    "rsi_mr":"mr","bollinger_z":"mr","crowd_panic":"mr","ma_distance":"mr","hurst":"mr",
    "ma_stack":"trend","macd_accel":"trend","adx_dir":"trend","price_cloud":"trend",
    "vol_ratio":"vol","obv_r2":"vol","accum_dist":"vol",
    "atr_pctile":"volat","bb_squeeze":"volat","rel_strength":"volat",
    "rev_momentum":"rev",
}
MICRO_MULT = {
    "TRENDING":{"mr":0.6,"trend":1.5,"vol":1.0,"volat":0.8,"rev":0.5},
    "REVERTING":{"mr":1.5,"trend":0.6,"vol":1.1,"volat":1.2,"rev":1.5},
    "MIXED":{"mr":1.0,"trend":1.0,"vol":1.0,"volat":1.0,"rev":1.0},
}
REGIME_MULT = {
    "EXPANSION":{"mr":0.8,"trend":1.3,"vol":1.0,"volat":0.8,"rev":0.7},
    "BULLISH":{"mr":0.9,"trend":1.2,"vol":1.0,"volat":0.9,"rev":0.8},
    "NEUTRAL":{"mr":1.0,"trend":1.0,"vol":1.0,"volat":1.0,"rev":1.0},
    "BEARISH":{"mr":1.3,"trend":0.7,"vol":1.1,"volat":1.2,"rev":1.3},
    "CRISIS":{"mr":1.5,"trend":0.5,"vol":1.2,"volat":1.4,"rev":1.5},
}

# Regime labels in score order (most bearish → most bullish)
_REGIME_ORDER = ["CRISIS", "RISK_OFF", "NEUTRAL", "RISK_ON", "EXPANSION"]
# Map macro_context.py labels → REGIME_MULT keys
_REGIME_ALIAS = {"RISK_ON": "BULLISH", "RISK_OFF": "BEARISH",
                 "CRISIS": "CRISIS", "NEUTRAL": "NEUTRAL", "EXPANSION": "EXPANSION"}

def _regime_blend(macro_score: float) -> dict:
    """
    Convert continuous macro_score [-1, 1] into a probability-weighted blend
    of cluster multipliers. Eliminates the hard threshold cliff where a score
    of 0.14 (NEUTRAL) vs 0.16 (BULLISH) caused a 20% weight shift on every factor.

    Each regime has a Gaussian centre on the [-1,1] scale:
      CRISIS=-1.0  RISK_OFF=-0.5  NEUTRAL=0.0  RISK_ON=0.5  EXPANSION=1.0
    Temperature σ=0.25 gives smooth overlap between adjacent regimes.
    Probabilities are softmax-normalised so they always sum to 1.
    """
    centres = {"CRISIS": -1.0, "RISK_OFF": -0.5, "NEUTRAL": 0.0,
               "RISK_ON": 0.5, "EXPANSION": 1.0}
    sigma = 0.25
    logits = {r: -0.5 * ((macro_score - c) / sigma) ** 2
              for r, c in centres.items()}
    max_l = max(logits.values())
    exp_l = {r: np.exp(v - max_l) for r, v in logits.items()}
    total = sum(exp_l.values())
    probs = {r: v / total for r, v in exp_l.items()}

    # Weighted blend across all five regime multiplier tables
    clusters = ["mr", "trend", "vol", "volat", "rev"]
    blended = {}
    for cl in clusters:
        blended[cl] = sum(
            probs[r] * REGIME_MULT[_REGIME_ALIAS.get(r, "NEUTRAL")][cl]
            for r in centres
        )
    return blended

class QuantSignalEngine:
    def __init__(self, cfg: RaptorConfig):
        self.cfg=cfg; self.rcfg=cfg.risk; self.f=Factors()
        self.adaptive=AdaptiveWeights(FACTOR_NAMES,os.path.dirname(os.path.abspath(__file__)))
    def _raw(self, sym, bars, spy_bars):
        c,v=bars["close"],bars["volume"]
        spy_c=spy_bars["close"] if spy_bars is not None else pd.Series(dtype=float)
        h,l=bars["high"],bars["low"]
        # Shared intermediates: compute EMA 8/21/50 and TR once, reuse across factors
        e8=c.ewm(span=8,adjust=False).mean()
        e21=c.ewm(span=21,adjust=False).mean()
        e50=c.ewm(span=50,adjust=False).mean()
        tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
        # ADX — one pass returns both dir_val and raw_val (was computed twice separately)
        pdm=h.diff().clip(lower=0); mdm=(-l.diff()).clip(lower=0)
        pdm[pdm<mdm]=0.0; mdm[mdm<pdm]=0.0
        atr_e=tr.ewm(span=14,adjust=False).mean()
        pdi=100*pdm.ewm(span=14,adjust=False).mean()/atr_e
        mdi=100*mdm.ewm(span=14,adjust=False).mean()/atr_e
        dx=100*(pdi-mdi).abs()/(pdi+mdi+1e-10)
        adx=dx.ewm(span=14,adjust=False).mean()
        adx_dir_val=float(adx.iloc[-1]*(1.0 if pdi.iloc[-1]>mdi.iloc[-1] else -1.0))
        adx_raw_val=float(adx.iloc[-1])
        # EMA-based factors inlined (share e8/e21/e50 series)
        e8l,e21l,e50l=e8.iloc[-1],e21.iloc[-1],e50.iloc[-1]
        ema_avg=(e8l+e21l+e50l)/3
        ma_dist=float(-(c.iloc[-1]-ema_avg)/ema_avg) if ema_avg!=0 else 0.0
        ma_order=float((e8l>e21l)+(e21l>e50l)-1)
        ma_slope=np.clip(sum((e.iloc[-1]/e.iloc[-5]-1) for e in [e8,e21,e50])/3*50,-0.4,0.4)
        ma_stk=float(ma_order*0.6+ma_slope)
        w_cloud=abs(e8l-e50l)
        pc=float((c.iloc[-1]-(e8l+e50l)/2)/w_cloud) if w_cloud>1e-10 else 0.0
        # TR-based factors inlined (share tr series)
        a_tr=tr.rolling(14).mean().dropna()
        atr14=float(a_tr.iloc[-1]) if len(a_tr)>0 else 0.0
        atr_pctl=float(-(scipy_stats.percentileofscore(a_tr.iloc[-60:].values,a_tr.iloc[-1])/100-0.5)*2) \
                 if len(a_tr)>=60 else np.nan
        rev_m=float((c.iloc[-1]-l.iloc[-3:].min())/atr14) \
              if (not pd.isna(atr14) and atr14>0) else np.nan
        return {
            "rsi_mr":self.f.rsi_mr(c),"bollinger_z":self.f.bollinger_z(c),
            "crowd_panic":self.f.crowd_panic(bars),"ma_distance":ma_dist,
            "hurst":self.f.hurst(c),"ma_stack":ma_stk,
            "macd_accel":self.f.macd_accel(c),"adx_dir":adx_dir_val,
            "price_cloud":pc,"vol_ratio":self.f.vol_ratio(v),
            "obv_r2":self.f.obv_r2(bars),"accum_dist":self.f.accum_dist(bars),
            "atr_pctile":atr_pctl,"bb_squeeze":self.f.bb_squeeze(c),
            "rel_strength":self.f.rel_strength(c,spy_c),
            "rev_momentum":rev_m,"_adx_raw":adx_raw_val,
        }
    def _detect_micro(self, hurst_raw, bars, adx_val=None):
        H=hurst_raw if not(isinstance(hurst_raw,float) and np.isnan(hurst_raw)) else 0.0
        actual_H=0.5-H; adx=adx_val if adx_val is not None else self.f.adx_raw(bars)
        if actual_H>0.55 and adx>25: return "TRENDING"
        elif actual_H<0.45 and adx<20: return "REVERTING"
        return "MIXED"
    def _market_scale(self, spy_bars):
        if spy_bars is None or len(spy_bars)<21: return 1.0
        spy_c=spy_bars["close"]
        roc_20=(spy_c.iloc[-1]/spy_c.iloc[-21])-1.0
        # Changepoint detection: 5-day vs 20-day momentum ratio
        # Sharp flip = regime transition, go defensive
        if len(spy_c)>=6:
            roc_5=(spy_c.iloc[-1]/spy_c.iloc[-6])-1.0
            # Momentum divergence: short-term flipped against long-term
            # TODO:DERIVE — all thresholds (0.01, 0.02) and scale values (0.5, 0.8, 1.0)
            # are round numbers. Derivation: compute regime-conditional entry win rate
            # across SPY ROC deciles in backtest; replace thresholds with empirical breaks.
            if roc_20>0.01 and roc_5<-0.02:  # Bull trend breaking
                return 0.5
            if roc_20<-0.01 and roc_5>0.02:  # Bear trend reversing (opportunity)
                return 1.0
        if roc_20>0.02: return 1.0
        elif roc_20>-0.02: return 0.8
        return 0.5
    def generate_signals(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime=macro_data.get("regime","NEUTRAL")
        # macro_score: continuous [-1,1] from macro_context.py classify_macro.
        # Falls back to a regime-centre estimate if running against old macro_context.json
        # that pre-dates this change (no macro_score key present).
        _regime_centres = {"RISK_ON":0.5,"BULLISH":0.5,"EXPANSION":0.75,
                           "NEUTRAL":0.0,"RISK_OFF":-0.5,"BEARISH":-0.5,"CRISIS":-1.0}
        macro_score=float(macro_data.get("macro_score",
                          _regime_centres.get(regime, 0.0)))
        if regime=="CRISIS" and self.rcfg.halt_in_crisis: return []
        market_scale=self._market_scale(spy_bars)
        raw,micros={},{}
        for sym,bars in bars_dict.items():
            if len(bars)<MIN_BARS_REQUIRED: continue
            try:
                r=self._raw(sym,bars,spy_bars); raw[sym]=r
                micros[sym]=self._detect_micro(r["hurst"],bars,adx_val=r["_adx_raw"])
            except: continue
        if len(raw)<10: return []
        syms=list(raw.keys()); zmat={}
        for fn in FACTOR_NAMES:
            vals=[raw[s].get(fn,np.nan) for s in syms]
            arr=np.array([v for v in vals if not(isinstance(v,float) and np.isnan(v))])
            if len(arr)<5:
                for s in syms: zmat.setdefault(s,{})[fn]=0.0
                continue
            mu=np.median(arr); sig=np.median(np.abs(arr-mu))*1.4826  # MAD-based robust std
            if sig<1e-10:
                for s in syms: zmat.setdefault(s,{})[fn]=0.0
                continue
            for i,s in enumerate(syms):
                v=vals[i]
                if isinstance(v,float) and np.isnan(v): zmat.setdefault(s,{})[fn]=0.0
                else: zmat.setdefault(s,{})[fn]=float(np.clip((v-mu)/sig,-3,3))
        # Inverse-vol weighting
        fd={fn:np.std([zmat[s][fn] for s in syms])+1e-6 for fn in FACTOR_NAMES}
        ivw={fn:1.0/fd[fn] for fn in FACTOR_NAMES}
        ivt=sum(ivw.values()); ivw={fn:v/ivt for fn,v in ivw.items()}
        # ── Factor covariance matrix (Ledoit-Wolf shrinkage) ─────────────────
        # Cross-sectional z-score matrix: rows=symbols, cols=factors
        # Used to compute per-symbol composite uncertainty: σ²_comp = w^T Σ w
        # Ledoit-Wolf (2004) analytical shrinkage toward scaled identity.
        # Prevents overconfident SNR estimates when factors are highly correlated.
        _Z=np.array([[zmat[s].get(fn,0.0) for fn in FACTOR_NAMES] for s in syms])
        _n,_p=_Z.shape  # n_symbols, n_factors
        _sample_cov=np.cov(_Z.T) if _n>_p+1 else np.eye(_p)
        _mu_cov=np.trace(_sample_cov)/_p
        # Oracle shrinkage intensity: grows when n_symbols is small relative to n_factors
        _alpha=float(np.clip((_p+2)/((_n-_p-1+1e-10)*_n),0.05,0.95))
        _factor_cov=(1.0-_alpha)*_sample_cov+_alpha*_mu_cov*np.eye(_p)
        logger.debug("FactorCov: n=%d p=%d alpha=%.3f trace=%.3f",_n,_p,_alpha,np.trace(_factor_cov))
        # Compute probability-weighted regime multiplier blend ONCE per scan.
        # _regime_blend() maps macro_score → continuous cluster weights.
        # No threshold cliffs — score 0.14 and 0.16 now produce nearly identical weights.
        _macro_m = _regime_blend(macro_score)
        logger.debug("RegimeBlend: score=%.3f mr=%.3f trend=%.3f vol=%.3f volat=%.3f rev=%.3f",
                     macro_score, _macro_m["mr"], _macro_m["trend"],
                     _macro_m["vol"], _macro_m["volat"], _macro_m["rev"])
        scored=[]; all_comp=[]
        for sym in syms:
            micro=micros.get(sym,"MIXED")
            macro_m=_macro_m  # probability-weighted blend, same for all symbols this scan
            micro_m=MICRO_MULT.get(micro,MICRO_MULT["MIXED"])
            cl=FACTOR_CLUSTERS
            w={fn:ivw[fn]*macro_m[cl[fn]]*micro_m[cl[fn]] for fn in FACTOR_NAMES}
            wt=sum(w.values()); w={fn:v/wt for fn,v in w.items()}
            w=self.adaptive.blend_weights(w)
            z=zmat[sym]
            # Soft shrinkage replaces the hard |z|>0.10 threshold filter.
            # Hard cutoff created score discontinuities: a factor at z=0.09 was
            # fully excluded; at z=0.11 it received full weight — a cliff that
            # caused noisy score jumps from threshold-straddling factors.
            # Soft shrinkage smoothly reduces small z-scores toward zero without
            # discrete inclusion/exclusion. Factors with |z|<0.10 contribute ≈0,
            # factors with |z|>0.50 are essentially unmodified.
            # Formula: z_soft[fn] = z[fn] × (|z[fn]| / (|z[fn]| + 0.10))
            # At |z|=0.10: 50% of z retained. At |z|=0.50: 83%. At |z|=1.0: 91%.
            z_soft={fn:z[fn]*(abs(z[fn])/(abs(z[fn])+0.10)) for fn in FACTOR_NAMES}
            aw_sum=sum(w[fn] for fn in FACTOR_NAMES)
            if aw_sum<1e-10: continue
            comp=sum(z_soft[fn]*w[fn]/aw_sum for fn in FACTOR_NAMES)
            all_comp.append(comp)
            contribs={fn:round(z_soft[fn]*w[fn]/aw_sum,6) for fn in FACTOR_NAMES}
            # ── Proper SNR: comp / sqrt(w^T Σ w) ────────────────────────────
            # σ²_comp = w^T × factor_cov × w accounts for factor correlation.
            # Correlated factors inflate composite score without adding information.
            # SNR correctly penalizes overconfident signals from correlated clusters.
            # Replaces heuristic comp/(std(z)+0.5) which was backwards:
            # high cross-factor disagreement inflated denominator, penalizing
            # precisely the signals where factors genuinely diverge.
            _w_vec=np.array([w.get(fn,0.0) for fn in FACTOR_NAMES])
            _comp_var=float(_w_vec@_factor_cov@_w_vec)
            _comp_std=np.sqrt(max(_comp_var,1e-8))
            snr=comp/_comp_std
            scored.append({"sym":sym,"comp":comp,"t":snr,"contribs":contribs,"micro":micro,"w":w})
        if not scored: return []
        # Sort on SNR — uncertainty-adjusted rank. Two stocks with identical composite
        # scores are separated by how much independent factor agreement supports them.
        scored.sort(key=lambda x:x["t"],reverse=True)
        # Store full signal map BEFORE top-N filter so hold_monitor can find held symbols
        # that have decayed out of the top. Without this, held symbols get _Dummy (FAR 0/16).
        self._last_full_signals = {
            s["sym"]: Signal(
                symbol=s["sym"], side="BUY",
                composite_score=round(s["comp"],4),
                composite_percentile=0.0,
                t_statistic=round(s["t"],4),
                factor_scores={fn:round(zmat[s["sym"]][fn],4) for fn in FACTOR_NAMES},
                factor_contributions=s["contribs"],
                factors_positive=sum(1 for fn in FACTOR_NAMES if zmat[s["sym"]][fn]>0),
                regime=f"{regime}/{micros.get(s['sym'],'MIXED')}",
                sentiment_score=0.0,
                atr=0.0, entry_price=0.0, stop_price=0.0, take_profit=0.0,
                kelly_fraction=0.0, hold_target_days=15,
                leverage_qualified=False, confirmation_type="adaptive",
                timestamp="",
            )
            for s in scored
        }
        top=[s for s in scored if s["comp"]>0][:self.cfg.execution.max_orders_per_scan*2]
        comp_arr=np.array(all_comp); signals=[]
        for s in top:
            sym=s["sym"]; bars=bars_dict[sym]
            entry=float(bars["close"].iloc[-1])
            atr_val=self.f.atr(bars,self.rcfg.atr_period)
            if atr_val<=0 or entry<=0: continue
            micro=s["micro"]
            stop_mult={"TRENDING":self.rcfg.initial_stop_atr_mult,"REVERTING":2.0,"MIXED":2.5}.get(micro,2.5)
            stop=round(max(entry-stop_mult*atr_val,0.01),2)
            # TODO:DERIVE — t/3.0 normalization: scales kelly from 0.5x (t=0) to 1.0x (t>=3).
            # 3.0 is a round number. Correct derivation: once 60+ IC-valid full exits exist,
            # regress realized pnl_pct ~ t_statistic and replace 3.0 with the empirical SNR
            # at which E[R] becomes meaningfully positive. Ref: Thorp 2006.
            base_kelly=self.rcfg.kelly_fraction*(0.5+min(abs(s["t"])/3.0,1.0))
            # TODO:DERIVE — kelly caps (0.02 floor, 0.12 ceiling) are round numbers.
            # Correct: max_f = f at max_DD_tolerance per Vince 1992. See kelly_engine.py
            # MAX_DD=0.15 bootstrap P25 output — activate when Kelly ACTIVE at 100 trades.
            kelly=float(np.clip(base_kelly*market_scale,0.02,0.12))
            if regime=="BEARISH": kelly*=self.rcfg.reduce_in_bearish
            rsi_raw=float(50*(1-raw[sym]["rsi_mr"]))  # rsi_mr=(50-RSI)/50, already computed
            bb_z=raw[sym]["bollinger_z"]
            lev=self.f.check_leverage(bars,spy_bars,rsi_raw,bb_z)
            if lev and abs(s["t"])>=2.0: kelly=min(kelly*2.0,0.20)
            pctile=scipy_stats.percentileofscore(comp_arr,s["comp"])/100.0
            atr_p=raw[sym].get("atr_pctile",0)
            # TODO:DERIVE — hold_target formula uses atr_pctile (volatility rank) as proxy
            # for hold duration. This conflates volatility with OU reversion speed.
            # Correct: hold_target = ln(2)/theta where theta is per-stock OU speed
            # (Leung & Zhang 2019). Constants 16 and 14 are also round numbers.
            hold=max(1,min(30,int(16+14*(atr_p if not(isinstance(atr_p,float) and np.isnan(atr_p)) else 0))))
            rev_m=raw[sym].get("rev_momentum",0)
            conf="reversal" if(isinstance(rev_m,(int,float)) and not np.isnan(rev_m) and rev_m>0.5) else "adaptive"
            signals.append(Signal(
                symbol=sym,side="BUY",composite_score=round(s["comp"],4),
                composite_percentile=round(pctile,4),t_statistic=round(s["t"],4),
                factor_scores={fn:round(zmat[sym][fn],4) for fn in FACTOR_NAMES},
                factor_contributions=s["contribs"],
                factors_positive=sum(1 for fn in FACTOR_NAMES if zmat[sym][fn]>0),
                regime=f"{regime}/{micro}",sentiment_score=0.0,atr=round(atr_val,4),
                entry_price=entry,stop_price=stop,take_profit=0.0,
                kelly_fraction=round(kelly,4),hold_target_days=hold,
                leverage_qualified=lev,confirmation_type=conf,
                timestamp=str(bars.index[-1]),
            ))
        signals.sort(key=lambda x:x.t_statistic,reverse=True)
        signals=signals[:self.cfg.execution.max_orders_per_scan]
        rc={}
        for m in micros.values(): rc[m]=rc.get(m,0)+1
        snr_vals=[s.t_statistic for s in signals]
        logger.info("v5.4 Signals: %d from %d | Macro=%s Scale=%.1f | Micro=%s | SNR min=%.2f max=%.2f",
                     len(signals),len(raw),regime,market_scale,rc,
                     min(snr_vals) if snr_vals else 0,max(snr_vals) if snr_vals else 0)
        # ── Write composite cache for velocity gate in main.py ───────────────
        # Stores today's composite score per symbol so tomorrow's scan can
        # compute composite_velocity = today - yesterday and gate on trajectory.
        # Atomic write — never leaves a partial file that would corrupt the gate.
        try:
            _cache={sym:round(sig.composite_score,4) for sym,sig in self._last_full_signals.items()}
            _cache_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"composite_cache.json")
            _tmp=_cache_path+".tmp"
            with open(_tmp,"w") as _f: json.dump(_cache,_f)
            os.replace(_tmp,_cache_path)
            logger.debug("CompCache: wrote %d symbol scores → composite_cache.json",len(_cache))
        except Exception as _ce:
            logger.warning("CompCache write failed: %s",_ce)
        return signals

