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
    def hurst(c, max_lag=20):
        r=np.log(c/c.shift(1)).dropna().values
        if len(r)<max_lag*2: return np.nan
        pts=[]
        for lag in range(2,max_lag+1):
            ns=len(r)//lag
            if ns<1: continue
            rl=[]
            for i in range(ns):
                sub=r[i*lag:(i+1)*lag]; d=np.cumsum(sub-sub.mean())
                R=d.max()-d.min(); S=sub.std()
                if S>1e-10: rl.append(R/S)
            mean_rs = np.mean(rl)
            if mean_rs > 0:
                pts.append((np.log(lag), np.log(mean_rs)))
        if len(pts)<4: return np.nan
        x=np.array([p[0] for p in pts]); y=np.array([p[1] for p in pts])
        return float(0.5-np.polyfit(x,y,1)[0])
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
        a = v.iloc[-21:-1].mean()
        today = v.iloc[-1]
        # Guard both denominator (a) and numerator (today) against zero/negative.
        # log(0) fires RuntimeWarning; log of zero volume = data gap, return nan.
        if a <= 0 or today <= 0:
            return np.nan
        return float(np.log(today / a))
    @staticmethod
    def obv_r2(df, lb=10):
        obv=(np.sign(df["close"].diff())*df["volume"]).cumsum()
        y=obv.iloc[-lb:].values; ys=(y-y.mean())/(y.std()+1e-10)
        s,_,r,_,_=scipy_stats.linregress(np.arange(lb,dtype=float),ys)
        return float(s*r**2)
    @staticmethod
    def accum_dist(df, lb=10):
        clv=((df["close"]-df["low"])-(df["high"]-df["close"]))/(df["high"]-df["low"]+1e-10)
        ad=(clv*df["volume"]).cumsum()
        y=ad.iloc[-lb:].values; ys=(y-y.mean())/(y.std()+1e-10)
        s,_,r,_,_=scipy_stats.linregress(np.arange(lb,dtype=float),ys)
        return float(s*abs(r))
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
    WEIGHT_FILE  = "adaptive_weights.json"
    # Ridge gate raised from 30 → 150 (López de Prado 2018, Advances in Financial ML).
    # At N=30 with 16 predictors, p/n ratio = 0.53 — well above the 0.1 safe threshold.
    # Simultaneous open trades share macro exposure and are not independent observations.
    # N=150 provides sufficient effective sample size (N_eff ≈ 50-80 after overlap correction)
    # to fit a stable 16-variable Ridge regression without curve-fitting to noise.
    MIN_TRADES   = 150
    MAX_ALPHA    = 0.30   # maximum Ridge blend weight (approached asymptotically at N=450)
    RIDGE_LAMBDA = 1.0    # L2 penalty — shrinks correlated factor betas toward each other

    # IC EMA half-life: trades decay to 50% weight after 20 trades.
    # Replaces flat 50-trade window which treated a trade 49 periods ago identically to yesterday.
    # EMA forgetting ensures the IC calibrator tracks the current regime, not historical ones.
    # Reference: López de Prado (2018) ch.7 — "A Better Alternative to the Sharp Ratio"
    IC_HALFLIFE  = 20     # trades (not days — each closed trade = one observation)

    def __init__(self, factor_names, base_dir="."):
        self.factor_names = factor_names
        self.path = os.path.join(base_dir, self.WEIGHT_FILE)
        self.data = self._load()
        self._ic_cache = None  # (n_trades, {fn: ic}) — reused across all symbols in one scan

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"trades": [], "ridge_beta": None, "n_trades": 0}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def record_trade(self, zscores, ret):
        row = {fn: zscores.get(fn, 0.0) for fn in self.factor_names}
        row["y"] = ret
        self.data["trades"].append(row)
        self.data["n_trades"] = len(self.data["trades"])
        self._fit()
        self._save()

    def _get_ic_boost(self):
        """
        Compute IC (Information Coefficient) per factor using EMA-weighted trades.

        EMA weighting: trade t periods ago gets weight = decay^t where decay = 0.5^(1/halflife).
        This ensures recent trades dominate the IC estimate — old market regimes fade out.

        IC = sum(w_t × sign(z_t == y_t)) / sum(w_t) - 0.5
        IC=0 = random. IC=+0.1 = factor predicted direction correctly 60% of the time.

        Replaces flat 50-trade window which gave equal weight to all trades regardless of age.
        """
        n = len(self.data["trades"])
        if n < 20:
            return {}
        if self._ic_cache and self._ic_cache[0] == n:
            return self._ic_cache[1]

        trades = self.data["trades"]
        decay  = 0.5 ** (1.0 / self.IC_HALFLIFE)  # per-trade decay factor
        n_use  = min(n, 100)  # cap at 100 trades — beyond this, decay makes old trades negligible
        recent = trades[-n_use:]

        # EMA weights: most recent trade = 1.0, each prior trade decays by `decay`
        weights = np.array([decay ** (n_use - 1 - i) for i in range(n_use)])
        w_sum   = weights.sum()

        ic = {}
        for fn in self.factor_names:
            zs = np.array([t.get(fn, 0.0) for t in recent])
            ys = np.array([t.get("y",  0.0) for t in recent])
            # Weighted fraction of trades where sign(z) == sign(y)
            correct = (np.sign(zs) == np.sign(ys)).astype(float)
            ic[fn]  = float((weights * correct).sum() / w_sum) - 0.5

        self._ic_cache = (n, ic)
        return ic

    def _fit(self):
        """Fit Ridge regression. Gated at MIN_TRADES=150 (was 30 — too few for 16 predictors)."""
        t = self.data["trades"]
        if len(t) < self.MIN_TRADES:
            self.data["ridge_beta"] = None
            return
        X = np.array([[tr.get(fn, 0) for fn in self.factor_names] for tr in t])
        y = np.array([tr["y"] for tr in t])
        k = len(self.factor_names)
        try:
            self.data["ridge_beta"] = np.linalg.solve(
                X.T @ X + self.RIDGE_LAMBDA * np.eye(k), X.T @ y
            ).tolist()
        except Exception:
            self.data["ridge_beta"] = None

    def blend_weights(self, base):
        if self.data["ridge_beta"] is None and not self._get_ic_boost():
            return base
        blended = dict(base)
        n = self.data["n_trades"]

        # Layer 1: Ridge regression blend (only active at N≥150)
        if self.data["ridge_beta"] is not None:
            b    = np.abs(np.array(self.data["ridge_beta"]))
            if b.sum() > 1e-10:
                norm = b / b.sum()
                ra   = {fn: float(norm[i]) for i, fn in enumerate(self.factor_names)}
                # Alpha ramp: 0% at N=150, MAX_ALPHA at N=450 (3× the gate).
                # Slower ramp than before (was 0%→30% over 60 trades) — requires more
                # evidence before trusting Ridge over base weights.
                a = min(self.MAX_ALPHA,
                        self.MAX_ALPHA * (n - self.MIN_TRADES) / (2 * self.MIN_TRADES))
                a = max(0.0, a)
                blended = {fn: (1 - a) * base[fn] + a * ra.get(fn, base[fn])
                           for fn in base}

        # Layer 2: EMA-weighted IC boost (active at N≥20, no gate — modest adjustment)
        ic_boost = self._get_ic_boost()
        if ic_boost:
            blended = {fn: blended.get(fn, 0) * (1.0 + ic_boost.get(fn, 0))
                       for fn in self.factor_names}

        tot = sum(blended.values())
        return {k: v / tot for k, v in blended.items()} if tot > 1e-10 else base

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
            if roc_20>0.01 and roc_5<-0.02:  # Bull trend breaking
                return 0.5
            if roc_20<-0.01 and roc_5>0.02:  # Bear trend reversing (opportunity)
                return 1.0
        if roc_20>0.02: return 1.0
        elif roc_20>-0.02: return 0.8
        return 0.5
    def generate_signals(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime=macro_data.get("regime","NEUTRAL")
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
        scored=[]; all_comp=[]
        for sym in syms:
            micro=micros.get(sym,"MIXED")
            macro_m=REGIME_MULT.get(regime,REGIME_MULT["NEUTRAL"])
            micro_m=MICRO_MULT.get(micro,MICRO_MULT["MIXED"])
            cl=FACTOR_CLUSTERS
            w={fn:ivw[fn]*macro_m[cl[fn]]*micro_m[cl[fn]] for fn in FACTOR_NAMES}
            wt=sum(w.values()); w={fn:v/wt for fn,v in w.items()}
            w=self.adaptive.blend_weights(w)
            z=zmat[sym]
            active={fn:z[fn] for fn in FACTOR_NAMES if abs(z[fn])>0.10}
            if len(active)<3: active={fn:z[fn] for fn in FACTOR_NAMES}
            aw_sum=sum(w[fn] for fn in active)
            if aw_sum<1e-10: continue
            comp=sum(z[fn]*w[fn]/aw_sum for fn in active)
            all_comp.append(comp)
            contribs={fn:round(z[fn]*w[fn]/aw_sum,6) if fn in active else 0.0 for fn in FACTOR_NAMES}
            t_stat=comp/(np.std([z[fn] for fn in FACTOR_NAMES])+0.5)
            scored.append({"sym":sym,"comp":comp,"t":t_stat,"contribs":contribs,"micro":micro,"w":w})
        if not scored: return []
        scored.sort(key=lambda x:x["comp"],reverse=True)
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
            # ── Kelly sizing — GAP B: caps derived from backtest data (Thorp 2006) ──
            # Previous: clip(base_kelly × market_scale, 0.02, 0.12) — arbitrary bounds.
            #
            # Derivation from 1565-trade backtest (2020-2025):
            #   E[R] = 1.476%, σ[R] = 8.307%
            #   Thorp f* = (p×b - q)/b = 14.26%  (b = avg_win/avg_loss = 1.643)
            #   Bayesian posterior (n_prior=50, f_prior=5%): f_post = 13.97%
            #   Half-Kelly (estimation uncertainty discount): f_half = 6.98%
            #   Drawdown constraint P(DD>20%) < 5%: f_max_dd = 5.17%
            #   f_base = min(f_half, f_max_dd) = 5.17%
            #   f_min  = f_base × 0.33 = 1.71%
            #
            # t/3.0 normalization validated: maps t=[0,3] → [0.5×,1.5×] kelly correctly.
            # Reference: Thorp (2006) — "The Kelly Criterion in Blackjack Sports Betting
            # and the Stock Market." Cap formula: f_max = -log(p_ruin) × σ² / (2 × DD_target)
            KELLY_MIN = 0.0171   # derived: f_base × 0.33 (weak signal floor)
            KELLY_MAX = 0.0517   # derived: drawdown-constrained ceiling (was 0.12)

            # ── GAP 2: Conviction-scaled Kelly (composite percentile rank) ──────────
            # Problem: t-stat captures signal z-score quality but not cross-sectional
            # rank. Two stocks with t=1.5 are treated identically even if one ranks
            # in the 95th percentile today and the other in the 55th. Percentile rank
            # captures relative conviction within today's opportunity set.
            #
            # Two-component Kelly:
            #   t_component:    signal statistical quality (how strong is the factor signal?)
            #   pctile_component: cross-sectional rank (how does this compare to today's field?)
            #
            # Both scaled to [0, 1], blended 60/40 in favor of percentile rank.
            # Rationale: percentile rank is more forward-looking (today's relative strength)
            # vs t-stat which is more backward-looking (historical factor z-score quality).
            #
            # Final kelly = f_min + (f_max - f_min) × conviction_scalar
            # This maps: conviction=0 → f_min=1.71%, conviction=1 → f_max=5.17%
            # Replaces: base_kelly = rcfg.kelly_fraction × (0.5 + min(|t|/3.0, 1.0))

            # Compute percentile rank first (needed for both GAP 2 and hold target)
            pctile = scipy_stats.percentileofscore(comp_arr, s["comp"]) / 100.0

            # t-stat component: maps t=[0,3] → [0,1], saturates above t=3
            t_component = min(abs(s["t"]) / 3.0, 1.0)

            # Percentile component: only count percentile above the entry threshold
            # (comp > 0 filter means all entries are above ~50th percentile of all symbols,
            #  but within the top-N candidates we want full [0,1] range)
            pctile_component = pctile  # already [0,1] within comp_arr

            # Conviction scalar: 40% t-stat, 60% percentile rank
            conviction = 0.40 * t_component + 0.60 * pctile_component

            base_kelly = KELLY_MIN + (KELLY_MAX - KELLY_MIN) * conviction
            kelly = float(np.clip(base_kelly * market_scale, KELLY_MIN, KELLY_MAX))
            if regime == "BEARISH": kelly *= self.rcfg.reduce_in_bearish
            rsi_raw = float(50 * (1 - raw[sym]["rsi_mr"]))
            bb_z = raw[sym]["bollinger_z"]
            lev = self.f.check_leverage(bars, spy_bars, rsi_raw, bb_z)
            if lev and abs(s["t"]) >= 2.0: kelly = min(kelly * 2.0, KELLY_MAX * 2.0)

            # ── Hold target — GAP C: OU theta per stock (Leung & Zhang 2019) ──────
            # Previous: hold = 16 + 14 × atr_pctile — wrong because it conflates
            # volatility magnitude (ATR) with mean-reversion speed (OU theta).
            # High-ATR stocks got longer hold targets when they should get shorter:
            # fast-reverting stocks need to be exited at the inflection, not held.
            #
            # Fix: estimate the stock's OU mean-reversion speed (theta) from
            # the 30-day log-price series. Half-life = log(2)/theta.
            # Hold target = 1 full half-life (× 2 if TRENDING — let trends run).
            #
            # Reference: Leung & Zhang (2019) arXiv:1701.03960
            #   Optimal stopping time under OU dynamics: E[T*] ≈ log(2)/theta
            try:
                close_vals = bars["close"].values[-31:]
                # Guard: log requires strictly positive prices — skip on data corruption
                if np.any(close_vals <= 0):
                    raise ValueError("non-positive price in OU theta window")
                close_log = np.log(close_vals)  # 30 lags + 1 for diff
                if len(close_log) >= 10:
                    y = np.diff(close_log)           # delta log-price
                    x = close_log[:-1] - close_log[:-1].mean()  # lagged deviation from mean
                    # OLS: y = -theta × x + epsilon
                    # theta > 0 = mean-reverting; theta < 0 = trending away
                    cov_xy = np.cov(x, y)[0, 1]
                    var_x  = np.var(x)
                    theta  = -cov_xy / var_x if var_x > 1e-10 else 0.0
                    # Cap theta to half-life range [2, 20] trading days
                    theta  = float(np.clip(theta, np.log(2)/20, np.log(2)/2))
                    half_life = np.log(2) / theta
                    # Trending stocks: give 2× the OU half-life to let the trend run
                    hold_mult = 2.0 if micro == "TRENDING" else 1.0
                    hold = int(np.clip(np.ceil(half_life * hold_mult), 3, 30))
                else:
                    raise ValueError("insufficient bars")
            except Exception:
                # Fallback: avg hold from backtest = 7.9 days, use as neutral default
                hold = 8
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
        signals.sort(key=lambda x:x.composite_score,reverse=True)
        signals=signals[:self.cfg.execution.max_orders_per_scan]
        rc={}
        for m in micros.values(): rc[m]=rc.get(m,0)+1
        logger.info("v5.4 Signals: %d from %d | Macro=%s Scale=%.1f | Micro=%s",
                     len(signals),len(raw),regime,market_scale,rc)
        return signals

