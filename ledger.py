"""
Raptor Position Ledger
======================
Tracks which model (v5.2 vs v6) owns which position.
Both models share one Alpaca account but maintain separate books.

Usage:
    from ledger import Ledger
    ledger = Ledger()
    ledger.record_entry("v5.2", "CVX", 20, 207.13, "2026-04-01")
    ledger.record_entry("v6",   "AAPL", 50, 253.00, "2026-04-01")
    ledger.get_positions("v5.2")  # Only v5.2 positions
    ledger.get_positions("v6")    # Only v6 positions
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional


LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "position_ledger.json")


class Ledger:

    def __init__(self, path: str = LEDGER_FILE):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"positions": {}, "closed": []}

    def _save(self):
        # Atomic write — tmp file + os.replace prevents corruption on crash
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def record_entry(self, model: str, symbol: str, shares: int,
                     entry_price: float, date: str, metadata: Dict = None):
        """Record a new position entry."""
        key = f"{model}:{symbol}"
        self.data["positions"][key] = {
            "model": model,
            "symbol": symbol,
            "shares": shares,
            "entry_price": entry_price,
            "entry_date": date,
            "metadata": metadata or {},
        }
        self._save()

    def record_exit(self, model: str, symbol: str, exit_price: float, date: str, reason: str):
        """Record a full position exit and move to closed list.

        FIX (2026-06-29, audit P0-1): the headline pnl/pnl_pct must reflect the
        WHOLE position, not just the shares remaining at this final leg. Every
        prior partial trim already recorded its own realized P&L in
        pos["trims"][i]["pnl_abs"] — that money is real and was already sold,
        but was previously dropped from the closed-trade headline figure that
        daily_recap.py and get_performance_summary() read. Confirmed against
        live position_ledger.json: this silently understated/overstated P&L
        on every multi-trim closed trade (e.g. MRVL true loss -$1,042.82 vs.
        previously-reported -$19.01; STM true gain +$3,805.56 vs. previously-
        reported +$2,238.33).
        """
        key = f"{model}:{symbol}"
        if key in self.data["positions"]:
            pos = self.data["positions"].pop(key)
            prior_trims      = pos.get("trims", [])
            prior_trim_pnl   = sum(t.get("pnl_abs", 0) for t in prior_trims)
            prior_trim_shares = sum(t.get("shares_sold", 0) for t in prior_trims)
            final_leg_shares = pos["shares"]
            final_leg_pnl    = (exit_price - pos["entry_price"]) * final_leg_shares
            total_shares     = prior_trim_shares + final_leg_shares

            pos["exit_price"]     = exit_price
            pos["exit_date"]      = date
            pos["exit_reason"]    = reason
            pos["exit_path"]      = reason  # mirror for outcome_tracker compatibility
            # final_leg_pnl/pnl_pct retained for debugging — NOT the headline figure
            pos["final_leg_pnl"]     = round(final_leg_pnl, 2)
            pos["final_leg_pnl_pct"] = round(((exit_price / pos["entry_price"]) - 1) * 100, 4)
            # entry_value: notional across ALL shares ever held in this position,
            # not just what's left at the final leg — required for both the
            # capital-efficiency metric and the corrected pnl_pct denominator below.
            pos["entry_value"] = round(pos["entry_price"] * total_shares, 2)
            # pos["pnl"] / pos["pnl_pct"] are the TRUE total across every trim plus
            # this final leg — this is the field daily_recap.py and
            # get_performance_summary() read as the trade's headline result.
            pos["pnl"]     = round(prior_trim_pnl + final_leg_pnl, 2)
            pos["pnl_pct"] = round((pos["pnl"] / pos["entry_value"]) * 100, 4) if pos["entry_value"] else 0.0
            # hold_days: calendar days from entry to exit — required by analytics
            try:
                from datetime import datetime as _dt
                _entry = _dt.strptime(str(pos.get("entry_date", ""))[:10], "%Y-%m-%d")
                _exit  = _dt.strptime(str(date)[:10], "%Y-%m-%d")
                pos["hold_days"] = (_exit - _entry).days
            except Exception:
                pos["hold_days"] = None
            # entry_regime: macro regime at entry time, stored in metadata by main.py
            pos["entry_regime"] = (pos.get("metadata") or {}).get("macro_regime")
            self.data["closed"].append(pos)
            self._save()
            return pos
        return None

    def record_trim(self, model: str, symbol: str, shares_sold: int,
                    trim_price: float, date: str, reason: str):
        """
        Record a partial position trim — reduces shares held, keeps position open.

        A math_trim sells a fraction of the position. The position stays in
        ledger["positions"] with reduced share count. Only record_exit moves
        a position to ledger["closed"]. Calling record_exit for a partial trim
        was the root cause of 8 positions disappearing from the ledger while
        remaining open in Alpaca.

        Returns the updated position dict, or None if key not found.
        """
        key = f"{model}:{symbol}"
        if key not in self.data["positions"]:
            return None

        pos = self.data["positions"][key]
        shares_before = pos.get("shares", 0)
        shares_after  = max(0, shares_before - shares_sold)

        # pnl on the trimmed shares, as percentage
        trim_pnl_pct = ((trim_price / pos["entry_price"]) - 1) * 100
        trim_pnl_abs = (trim_price - pos["entry_price"]) * shares_sold

        # Log the partial exit in trim history inside the position record
        trim_record = {
            "date":          date,
            "reason":        reason,
            "shares_sold":   shares_sold,
            "trim_price":    trim_price,
            "pnl_pct":       round(trim_pnl_pct, 4),
            "pnl_abs":       round(trim_pnl_abs, 2),
            "shares_before": shares_before,
            "shares_after":  shares_after,
        }
        pos.setdefault("trims", []).append(trim_record)
        pos["shares"] = shares_after

        if shares_after == 0:
            # All shares trimmed away — move to closed.
            #
            # FIX (2026-06-29, audit P0-1): trim_record for THIS trim was already
            # appended to pos["trims"] above, so pos["trims"] now contains every
            # leg of this position's full realized P&L with no remainder. The
            # previous code used only this final trim's own shares/price, silently
            # dropping every earlier trim's P&L from the headline closed-trade
            # figure that daily_recap.py and get_performance_summary() read.
            total_shares_orig = sum(t.get("shares_sold", 0) for t in pos["trims"])
            total_pnl         = sum(t.get("pnl_abs", 0) for t in pos["trims"])

            pos["exit_price"]  = trim_price
            pos["exit_date"]   = date
            pos["exit_reason"] = reason
            pos["exit_path"]   = reason
            # final_leg_pnl_pct retained for debugging — NOT the headline figure
            pos["final_leg_pnl"]     = round(trim_pnl_abs, 2)
            pos["final_leg_pnl_pct"] = round(trim_pnl_pct, 4)
            # pos["pnl"] / pos["pnl_pct"] are the TRUE total across every trim
            # (this position closed entirely via trims, so the sum of all trims'
            # pnl_abs already represents the full realized result).
            pos["entry_value"] = round(pos["entry_price"] * total_shares_orig, 2)
            pos["pnl"]         = round(total_pnl, 2)
            pos["pnl_pct"]     = round((pos["pnl"] / pos["entry_value"]) * 100, 4) if pos["entry_value"] else 0.0
            # hold_days, entry_regime — same as record_exit
            try:
                from datetime import datetime as _dt
                _entry = _dt.strptime(str(pos.get("entry_date", ""))[:10], "%Y-%m-%d")
                _exit  = _dt.strptime(str(date)[:10], "%Y-%m-%d")
                pos["hold_days"] = (_exit - _entry).days
            except Exception:
                pos["hold_days"] = None
            pos["entry_regime"] = (pos.get("metadata") or {}).get("macro_regime")
            self.data["positions"].pop(key)
    