"""Check account positions and buying power."""
from config import CONFIG
from data_feeds import AlpacaDataFeed

a = AlpacaDataFeed(CONFIG)
acct = a.get_account()

print(f"Equity:       ${acct['equity']:,.2f}")
print(f"Cash:         ${acct['cash']:,.2f}")
print(f"Buying power: ${acct['buying_power']:,.2f}")
print()
print("Positions:")
for p in a.get_positions():
    value = p['qty'] * p['current_price']
    print(f"  {p['symbol']:8s}  qty={p['qty']:>10}  value=${value:>10,.2f}  pnl={p['unrealized_pnl_pct']*100:>+6.1f}%")
