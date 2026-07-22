"""
test_data_feeds_safety.py — Permanent regression test for data_feeds.py's
Alpaca null-field handling.

WHY THIS EXISTS (2026-07-06): daily_recap.py, exit_monitor.py, and main.py all
crashed the same day because Alpaca returned daytrade_count=None and
data_feeds.py's get_account() did a bare int(acct.daytrade_count). The first
fix defaulted EVERY field to 0/0.0 on None — which silently turned a missing
equity/cash/qty/price into a fabricated-looking-real number, exactly what
RAPTOR_MASTER_PLAN.md's "Data integrity corollary" prohibits ("Real data or
skip. Never fabricate a fallback value that looks real"). The real fix:
capital- and position-critical fields (equity, cash, buying_power,
portfolio_value, qty, avg_entry_price, current_price) now RAISE
AlpacaDataError instead of defaulting; purely informational fields
(daytrade_count, unrealized_pnl, unrealized_pnl_pct) still default safely.

This test locks that contract in place. If a future edit to data_feeds.py
accidentally reverts a critical field back to a silent default (or adds a new
field without deciding which category it belongs to), this test fails loudly
instead of the bug being discovered live at 9:35 AM again.

Run: python test_data_feeds_safety.py
Exits 0 with "ALL PASSED" on success, exits 1 and prints failures otherwise.
No pytest dependency — plain asserts, so it runs anywhere Python does.
"""

import sys

from data_feeds import AlpacaDataFeed, AlpacaDataError


class _FakeTradingClient:
    """Stand-in for alpaca.trading.client.TradingClient — only the two
    methods AlpacaDataFeed.get_account()/get_positions() actually call."""
    def __init__(self, acct=None, positions=None):
        self._acct = acct
        self._positions = positions or []

    def get_account(self):
        return self._acct

    def get_all_positions(self):
        return self._positions


class _FakeAcct:
    def __init__(self, equity=100000.0, cash=50000.0, buying_power=150000.0,
                 portfolio_value=100000.0, daytrade_count=2):
        self.equity = equity
        self.cash = cash
        self.buying_power = buying_power
        self.portfolio_value = portfolio_value
        self.daytrade_count = daytrade_count


class _FakePosition:
    def __init__(self, symbol="AAPL", qty=10.0, avg_entry_price=150.0,
                 current_price=160.0, unrealized_pl=100.0,
                 unrealized_plpc=0.05, side="long"):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price
        self.unrealized_pl = unrealized_pl
        self.unrealized_plpc = unrealized_plpc
        self.side = side


def _feed(acct=None, positions=None):
    """Build an AlpacaDataFeed without running its real __init__ (which
    constructs live alpaca SDK clients and needs real API keys) — just wire
    a fake trading_client onto a bare instance."""
    feed = AlpacaDataFeed.__new__(AlpacaDataFeed)
    feed.trading_client = _FakeTradingClient(acct=acct, positions=positions)
    return feed


_results = []


def check(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except AssertionError as e:
        _results.append((name, False, f"assertion failed: {e}"))
    except Exception as e:
        _results.append((name, False, f"unexpected exception: {e!r}"))


# ── get_account(): critical fields must raise on None ───────────────────────

def t_daytrade_count_none_defaults():
    acct = _feed(acct=_FakeAcct(daytrade_count=None)).get_account()
    assert acct["day_trade_count"] == 0


def t_equity_none_raises():
    try:
        _feed(acct=_FakeAcct(equity=None)).get_account()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "account.equity" in str(e)


def t_cash_none_raises():
    try:
        _feed(acct=_FakeAcct(cash=None)).get_account()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "account.cash" in str(e)


def t_buying_power_none_raises():
    try:
        _feed(acct=_FakeAcct(buying_power=None)).get_account()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "account.buying_power" in str(e)


def t_portfolio_value_none_raises():
    try:
        _feed(acct=_FakeAcct(portfolio_value=None)).get_account()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "account.portfolio_value" in str(e)


def t_healthy_account_exact_values():
    acct = _feed(acct=_FakeAcct()).get_account()
    assert acct == {
        "equity": 100000.0, "cash": 50000.0, "buying_power": 150000.0,
        "portfolio_value": 100000.0, "day_trade_count": 2,
    }, acct


# ── get_positions(): critical fields must raise on None ─────────────────────

def t_position_qty_none_raises():
    try:
        _feed(positions=[_FakePosition(symbol="AAL", qty=None)]).get_positions()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "AAL.qty" in str(e)


def t_position_avg_entry_none_raises():
    try:
        _feed(positions=[_FakePosition(symbol="WULF", avg_entry_price=None)]).get_positions()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "WULF.avg_entry_price" in str(e)


def t_position_current_price_none_raises():
    try:
        _feed(positions=[_FakePosition(symbol="BAC", current_price=None)]).get_positions()
        assert False, "expected AlpacaDataError"
    except AlpacaDataError as e:
        assert "BAC.current_price" in str(e)


def t_position_unrealized_fields_default():
    positions = _feed(positions=[_FakePosition(unrealized_pl=None, unrealized_plpc=None)]).get_positions()
    assert positions[0]["unrealized_pnl"] == 0.0
    assert positions[0]["unrealized_pnl_pct"] == 0.0


def t_healthy_position_exact_values():
    positions = _feed(positions=[_FakePosition()]).get_positions()
    assert positions == [{
        "symbol": "AAPL", "qty": 10.0, "avg_entry": 150.0, "current_price": 160.0,
        "unrealized_pnl": 100.0, "unrealized_pnl_pct": 0.05, "side": "long",
    }], positions


# ── One bad position must not silently drop just that symbol — the whole
#    call raises, matching get_account()'s fail-closed behavior. A caller
#    reading a positions list with one symbol quietly missing could treat a
#    real, still-open position as flat (duplicate-entry / missed-stop risk).

def t_one_bad_position_among_good_ones_still_raises():
    good = _FakePosition(symbol="GOOD")
    bad = _FakePosition(symbol="BAD", current_price=None)
    try:
        _feed(positions=[good, bad]).get_positions()
        assert False, "expected AlpacaDataError even with other valid positions present"
    except AlpacaDataError as e:
        assert "BAD.current_price" in str(e)


def main():
    tests = [
        ("daytrade_count=None -> defaults to 0, no raise", t_daytrade_count_none_defaults),
        ("equity=None -> raises AlpacaDataError", t_equity_none_raises),
        ("cash=None -> raises AlpacaDataError", t_cash_none_raises),
        ("buying_power=None -> raises AlpacaDataError", t_buying_power_none_raises),
        ("portfolio_value=None -> raises AlpacaDataError", t_portfolio_value_none_raises),
        ("healthy account -> exact expected dict", t_healthy_account_exact_values),
        ("position qty=None -> raises AlpacaDataError", t_position_qty_none_raises),
        ("position avg_entry_price=None -> raises AlpacaDataError", t_position_avg_entry_none_raises),
        ("position current_price=None -> raises AlpacaDataError", t_position_current_price_none_raises),
        ("position unrealized_pnl/pct=None -> defaults, no raise", t_position_unrealized_fields_default),
        ("healthy position -> exact expected dict", t_healthy_position_exact_values),
        ("one bad position among good ones -> still raises (whole call)", t_one_bad_position_among_good_ones_still_raises),
    ]
    for name, fn in tests:
        check(name, fn)

    print()
    n_pass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, err in _results:
        status = "PASS" if ok else "FAIL"
        line = f"{status}: {name}"
        if err:
            line += f"\n       {err}"
        print(line)
    print(f"\n{n_pass}/{len(_results)} passed")

    if n_pass != len(_results):
        print("\nFAILED — data_feeds.py's null-safety contract has regressed. "
              "See this file's module docstring before changing get_account()/get_positions().")
        sys.exit(1)
    print("\nALL PASSED")


if __name__ == "__main__":
    main()
