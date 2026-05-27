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
        """Record a full position exit and move to closed list."""
        key = f"{model}:{symbol}"
        if key in self.data["positions"]:
            pos = self.data["positions"].pop(key)
            pos["exit_price"]  = exit_price
            pos["exit_date"]   = date
            pos["exit_reason"] = reason
            pos["exit_path"]   = reason  # mirror for outcome_tracker compatibility
            pos["pnl"]         = (exit_price - pos["entry_price"]) * pos["shares"]
            # pnl_pct as percentage (e.g. 5.2 = 5.2%), NOT raw decimal (0.052)
            pos["pnl_pct"]     = ((exit_price / pos["entry_price"]) - 1) * 100
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
            # All shares trimmed away — move to closed
            pos["exit_price"]  = trim_price
            pos["exit_date"]   = date
            pos["exit_reason"] = reason
            pos["exit_path"]   = reason
            pos["pnl_pct"]     = round(trim_pnl_pct, 4)
            pos["pnl"]         = round((trim_price - pos["entry_price"]) * shares_before, 2)
            self.data["positions"].pop(key)
            self.data["closed"].append(pos)

        self._save()
        return pos

    def get_positions(self, model: str) -> List[Dict]:
        """Get all open positions for a specific model."""
        return [v for v in self.data["positions"].values() if v["model"] == model]

    def get_held_symbols(self, model: str) -> set:
        """Get set of symbols currently held by a model."""
        return {v["symbol"] for v in self.data["positions"].values() if v["model"] == model}

    def get_all_held_symbols(self) -> set:
        """Get all held symbols across all models."""
        return {v["symbol"] for v in self.data["positions"].values()}

    def get_closed(self, model: str = None) -> List[Dict]:
        """Get closed trades, optionally filtered by model."""
        if model:
            return [t for t in self.data["closed"] if t["model"] == model]
        return self.data["closed"]

    def get_equity_allocation(self, model: str, total_equity: float,
                              allocation_pct: float = 0.50) -> float:
        """
        How much equity a model is allowed to use.
        Default: 50/50 split between two models.
        """
        return total_equity * allocation_pct

    def get_performance_summary(self, model: str) -> Dict:
        """Quick P&L summary for a model."""
        closed = self.get_closed(model)
        if not closed:
            return {"trades": 0, "net_pnl": 0, "win_rate": 0}
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        return {
            "trades": len(closed),
            "winners": len(winners),
            "win_rate": round(len(winners) / len(closed) * 100, 1),
            "net_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(closed), 2),
        }

    def print_status(self):
        """Print side-by-side model comparison."""
        models = set(v["model"] for v in self.data["positions"].values())
        models.update(t["model"] for t in self.data["closed"])

        print("=" * 65)
        print("  RAPTOR A/B MODEL COMPARISON")
        print("=" * 65)

        for m in sorted(models):
            positions = self.get_positions(m)
            perf = self.get_performance_summary(m)
            print(f"\n  Model: {m}")
            print(f"    Open positions: {len(positions)}")
            for p in positions:
                print(f"      {p['symbol']:6s}  {p['shares']} shares @ ${p['entry_price']:.2f}  ({p['entry_date']})")
            print(f"    Closed trades:  {perf['trades']}")
            print(f"    Win rate:       {perf['win_rate']}%")
            print(f"    Net P&L:        ${perf['net_pnl']:,.2f}")

        print("")
        print("=" * 65)


if __name__ == "__main__":
    ledger = Ledger()
    ledger.print_status()
