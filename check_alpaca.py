from data_feeds import DataManager
from config import CONFIG

dm = DataManager(CONFIG)

print("=== ALPACA POSITIONS ===")
pos = dm.alpaca.get_positions()
print(f"Open positions: {len(pos)}")
for p in pos:
    # alpaca-py returns Position objects — print all available keys first
    d = p if isinstance(p, dict) else vars(p)
    symbol      = d.get("symbol") or getattr(p, "symbol", "?")
    qty         = d.get("qty") or getattr(p, "qty", "?")
    entry       = d.get("avg_entry_price") or d.get("cost_basis") or getattr(p, "avg_entry_price", getattr(p, "cost_basis", "?"))
    plpc        = d.get("unrealized_plpc") or getattr(p, "unrealized_plpc", None)
    mkt         = d.get("market_value") or getattr(p, "market_value", None)
    pnl_str     = f"{float(plpc)*100:+.2f}%" if plpc is not None else "?"
    mkt_str     = f"${float(mkt):,.0f}" if mkt is not None else "?"
    print(f"  {symbol:6s}  qty={qty}  entry={entry}  pnl={pnl_str}  mkt={mkt_str}")

print()
print("=== ALPACA ACCOUNT ===")
acct = dm.alpaca.get_account()
a = acct if isinstance(acct, dict) else vars(acct)
print(f"  Equity:        ${float(a.get('equity', 0)):,.2f}")
print(f"  Cash:          ${float(a.get('cash', 0)):,.2f}")
print(f"  Buying power:  ${float(a.get('buying_power', 0)):,.2f}")
print(f"  Portfolio val: ${float(a.get('portfolio_value', 0)):,.2f}")

print()
print("=== RAW KEYS (first position) ===")
if pos:
    d = pos[0] if isinstance(pos[0], dict) else vars(pos[0])
    for k, v in d.items():
        print(f"  {k}: {v}")
