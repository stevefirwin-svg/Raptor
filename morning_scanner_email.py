"""
morning_scanner_email.py — RAPTOR Morning Scanner Summary Email
Runs after main.py (9:35 AM scan) to report what happened:
  - New positions opened today
  - Trims executed today
  - Full exits today
  - All currently held positions

Usage:
  python morning_scanner_email.py          # Send email
  python morning_scanner_email.py --preview  # Save HTML locally, no send
"""

import json
import os
import sys
import smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Config ─────────────────────────────────────────────────────────────────────
EMAIL_SENDER   = "stevefirwin@gmail.com"
EMAIL_RECEIVER = "stevefirwin@gmail.com"
EMAIL_PASSWORD = "trhy qqzo kylt jker"

LEDGER_FILE    = "position_ledger.json"
TRIM_LOG_FILE  = "trim_log.json"
OUTCOME_FILE   = "outcome_log.json"

TODAY = date.today().isoformat()  # e.g. "2026-05-29"

# ── Colour palette (email-safe) ─────────────────────────────────────────────────
BG_DARK    = "#0d0f14"
BG_CARD    = "#13161e"
BG_ROW_ALT = "#191c26"
BORDER     = "#23283a"
ACCENT     = "#4f8ef7"
GREEN      = "#3dd68c"
RED        = "#f75f5f"
AMBER      = "#f7b955"
TEXT_MAIN  = "#e8eaf0"
TEXT_DIM   = "#7b829a"
FONT       = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"


# ── Data loading ────────────────────────────────────────────────────────────────

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def get_today_trims():
    """All trim events from trim_log.json dated today."""
    raw = load_json(TRIM_LOG_FILE, [])
    today_trims = []
    for t in raw:
        ts = t.get("timestamp", "")
        if ts.startswith(TODAY):
            today_trims.append(t)
    return today_trims


def get_today_exits():
    """Full exits (from outcome_log.json) where exit_date is today."""
    raw = load_json(OUTCOME_FILE, [])
    exits = []
    for t in raw:
        exit_date = t.get("exit_date", "")
        if exit_date.startswith(TODAY):
            exits.append(t)
    return exits


def get_held_positions():
    """All open positions from position_ledger.json, sorted by symbol."""
    ledger = load_json(LEDGER_FILE, {})
    positions = ledger.get("positions", {})
    held = []
    for key, val in positions.items():
        sym = val.get("symbol", key)
        entry_date = val.get("entry_date", "")
        held.append({
            "symbol": sym,
            "entry_date": entry_date,
            "new_today": entry_date == TODAY,
        })
    held.sort(key=lambda x: x["symbol"])
    return held


def get_new_entries():
    """Positions opened today (entry_date == today)."""
    return [p for p in get_held_positions() if p["new_today"]]


# ── HTML builders ───────────────────────────────────────────────────────────────

def _section_header(title, count, colour=ACCENT):
    badge = (
        f'<span style="background:{colour}22;color:{colour};'
        f'font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;'
        f'margin-left:10px;letter-spacing:0.04em;">{count}</span>'
    )
    return f"""
    <tr>
      <td colspan="2" style="padding:28px 0 10px 0;">
        <span style="font-size:13px;font-weight:700;letter-spacing:0.08em;
                     text-transform:uppercase;color:{TEXT_DIM};">{title}</span>
        {badge}
      </td>
    </tr>"""


def _pill(text, bg, color):
    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;'
        f'letter-spacing:0.03em;">{text}</span>'
    )


def _symbol_chip(sym, colour=ACCENT):
    return (
        f'<span style="font-family:\'Courier New\',monospace;font-size:15px;'
        f'font-weight:800;color:{colour};letter-spacing:0.04em;">{sym}</span>'
    )


def _divider():
    return f'<tr><td colspan="2"><div style="height:1px;background:{BORDER};margin:4px 0;"></div></td></tr>'


def build_symbol_rows(items, colour, label_fn, right_fn=None):
    """Generic row builder for a list of symbol items."""
    rows = ""
    for i, item in enumerate(items):
        bg = BG_ROW_ALT if i % 2 == 0 else BG_CARD
        right_html = right_fn(item) if right_fn else ""
        rows += f"""
        <tr style="background:{bg};">
          <td style="padding:12px 16px;border-radius:6px 0 0 6px;">
            {_symbol_chip(item['symbol'], colour)}
            <span style="font-size:12px;color:{TEXT_DIM};margin-left:10px;">{label_fn(item)}</span>
          </td>
          <td style="padding:12px 16px;text-align:right;border-radius:0 6px 6px 0;">
            {right_html}
          </td>
        </tr>
        <tr><td colspan="2" style="padding:1px 0;"></td></tr>"""
    return rows


def build_html(new_entries, trims, exits, held):
    now_str = datetime.now().strftime("%A, %B %#d · %I:%M %p ET")
    market_decision = load_json("market_decision.json", {})
    regime = market_decision.get("macro_regime", "—")
    scan_mode = market_decision.get("decision", "—")

    regime_colour = GREEN if "ON" in regime else (RED if "OFF" in regime else AMBER)
    scan_colour   = GREEN if scan_mode == "SCAN" else (RED if scan_mode == "STANDBY" else AMBER)

    # ── new entries section ──
    entry_rows = ""
    if new_entries:
        entry_rows += _section_header("New Positions", len(new_entries), GREEN)
        entry_rows += build_symbol_rows(
            new_entries,
            colour=GREEN,
            label_fn=lambda x: "entered today",
            right_fn=lambda x: _pill("NEW", f"{GREEN}22", GREEN),
        )
    else:
        entry_rows += _section_header("New Positions", 0, GREEN)
        entry_rows += f"""<tr><td colspan="2" style="padding:12px 16px;color:{TEXT_DIM};font-size:13px;">No new entries today.</td></tr>"""

    # ── trims section ──
    trim_rows = ""
    if trims:
        # deduplicate: if same symbol trimmed multiple times, show once with last reason
        seen = {}
        for t in trims:
            seen[t["symbol"]] = t
        trim_items = [{"symbol": s, **v} for s, v in seen.items()]
        trim_items.sort(key=lambda x: x["symbol"])

        trim_rows += _section_header("Trimmed Today", len(trim_items), AMBER)
        trim_rows += build_symbol_rows(
            trim_items,
            colour=AMBER,
            label_fn=lambda x: x.get("reason", "trim"),
            right_fn=lambda x: _pill("TRIM", f"{AMBER}22", AMBER),
        )
    else:
        trim_rows += _section_header("Trimmed Today", 0, AMBER)
        trim_rows += f"""<tr><td colspan="2" style="padding:12px 16px;color:{TEXT_DIM};font-size:13px;">No trims today.</td></tr>"""

    # ── exits section ──
    exit_rows = ""
    if exits:
        # deduplicate by symbol (multiple partial exits from trim → closed)
        seen = {}
        for e in exits:
            seen[e["symbol"]] = e
        exit_items = [{"symbol": s, **v} for s, v in seen.items()]
        exit_items.sort(key=lambda x: x["symbol"])

        exit_rows += _section_header("Closed Today", len(exit_items), RED)
        exit_rows += build_symbol_rows(
            exit_items,
            colour=RED,
            label_fn=lambda x: x.get("actual_exit_path", "exit"),
            right_fn=lambda x: _pill("CLOSED", f"{RED}22", RED),
        )
    else:
        exit_rows += _section_header("Closed Today", 0, RED)
        exit_rows += f"""<tr><td colspan="2" style="padding:12px 16px;color:{TEXT_DIM};font-size:13px;">No positions closed today.</td></tr>"""

    # ── held positions grid ──
    held_chips = ""
    for p in held:
        chip_colour = GREEN if p["new_today"] else ACCENT
        held_chips += (
            f'<span style="display:inline-block;margin:4px 5px;'
            f'padding:7px 14px;border-radius:8px;'
            f'background:{chip_colour}18;border:1px solid {chip_colour}44;'
            f'font-family:\'Courier New\',monospace;font-size:14px;'
            f'font-weight:800;color:{chip_colour};letter-spacing:0.04em;">'
            f'{p["symbol"]}</span>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAPTOR Morning Scan</title>
</head>
<body style="margin:0;padding:0;background:{BG_DARK};font-family:{FONT};">

<table width="100%" cellpadding="0" cellspacing="0" style="background:{BG_DARK};padding:32px 0;">
<tr><td align="center">

  <!-- Outer card -->
  <table width="580" cellpadding="0" cellspacing="0"
         style="background:{BG_CARD};border-radius:16px;
                border:1px solid {BORDER};overflow:hidden;max-width:580px;">

    <!-- Header bar -->
    <tr>
      <td style="background:linear-gradient(135deg,#1a1f30 0%,#0d0f14 100%);
                 padding:28px 32px 24px 32px;border-bottom:1px solid {BORDER};">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;
                          text-transform:uppercase;color:{TEXT_DIM};margin-bottom:6px;">
                RAPTOR v5.5
              </div>
              <div style="font-size:22px;font-weight:800;color:{TEXT_MAIN};letter-spacing:-0.01em;">
                Morning Scanner
              </div>
              <div style="font-size:12px;color:{TEXT_DIM};margin-top:4px;">{now_str}</div>
            </td>
            <td align="right" valign="top">
              <div style="text-align:right;">
                <div style="margin-bottom:6px;">
                  {_pill(regime, f'{regime_colour}22', regime_colour)}
                </div>
                <div>
                  {_pill(scan_mode, f'{scan_colour}22', scan_colour)}
                </div>
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Body -->
    <td style="padding:8px 32px 32px 32px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:0 0;">

        {entry_rows}
        {trim_rows}
        {exit_rows}

        <!-- Held positions -->
        <tr>
          <td colspan="2" style="padding:28px 0 10px 0;">
            <span style="font-size:13px;font-weight:700;letter-spacing:0.08em;
                         text-transform:uppercase;color:{TEXT_DIM};">Currently Held</span>
            <span style="background:{ACCENT}22;color:{ACCENT};font-size:11px;font-weight:700;
                         padding:2px 8px;border-radius:20px;margin-left:10px;">{len(held)}</span>
          </td>
        </tr>
        <tr>
          <td colspan="2" style="padding:4px 0 0 0;">
            <div style="background:{BG_ROW_ALT};border-radius:10px;padding:12px 10px;
                        line-height:2;">
              {held_chips if held_chips else f'<span style="color:{TEXT_DIM};font-size:13px;">No open positions.</span>'}
            </div>
          </td>
        </tr>

      </table>
    </td>

    <!-- Footer -->
    <tr>
      <td style="padding:16px 32px;border-top:1px solid {BORDER};
                 background:#0a0c11;">
        <span style="font-size:11px;color:{TEXT_DIM};">
          Auto-generated by RAPTOR · morning_scanner_email.py
        </span>
      </td>
    </tr>

  </table>

</td></tr>
</table>

</body>
</html>"""

    return html


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    preview_mode = "--preview" in sys.argv

    new_entries = get_new_entries()
    trims       = get_today_trims()
    exits       = get_today_exits()
    held        = get_held_positions()

    print(f"[morning_scanner_email] {TODAY}")
    print(f"  New entries : {[e['symbol'] for e in new_entries]}")
    print(f"  Trims today : {list({t['symbol'] for t in trims})}")
    print(f"  Exits today : {list({e['symbol'] for e in exits})}")
    print(f"  Held        : {[p['symbol'] for p in held]}")

    html = build_html(new_entries, trims, exits, held)

    if preview_mode:
        out = f"morning_scanner_preview_{TODAY}.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Preview saved → {out}")
        return

    # Send email
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg["Subject"] = f"RAPTOR Morning Scan · {datetime.now().strftime('%b %#d')} · {len(held)} held"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.send_message(msg)
        print("Morning scan email sent.")
    except Exception as e:
        import traceback
        print(f"Email failed: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
