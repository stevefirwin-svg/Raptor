import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

EMAIL_SENDER   = "stevefirwin@gmail.com"
EMAIL_PASSWORD = ""  # SECURITY 2026-06-10: hardcoded app password removed — archived file, do not use
EMAIL_RECEIVER = "stevefirwin@gmail.com"

html = """
<html>
<body style="margin:0;padding:0;background:#0a0a1a;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:800px;margin:0 auto;background:#12122a;border:1px solid #2a2a3e">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a1a3e,#0d0d2b);padding:24px 28px;border-bottom:2px solid #00d4aa">
    <div style="color:#00d4aa;font-size:13px;letter-spacing:3px;text-transform:uppercase;font-weight:700">RAPTOR v5.4</div>
    <div style="color:#e0e0e0;font-size:22px;font-weight:700;margin-top:4px">System Architecture Ontology</div>
    <div style="color:#a0a0b0;font-size:12px;margin-top:4px">Generated """ + datetime.now().strftime("%B %d, %Y %I:%M %p ET") + """</div>
  </div>

  <!-- Intro -->
  <div style="padding:20px 28px;border-bottom:1px solid #2a2a3e;color:#a0a0b0;font-size:13px;line-height:1.6">
    Full architectural ontology of the Raptor trading system. Every layer, every file, every data flow — from the session gate at 9:00 AM through to outcome collection and the planned prompt calibration engine.
  </div>

  <!-- Layer Key -->
  <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px">Layer Map</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr>
        <td style="padding:6px 10px;color:#9b8fd4;font-weight:700">LAYER 4</td>
        <td style="padding:6px 10px;color:#e0e0e0">Session Gate</td>
        <td style="padding:6px 10px;color:#a0a0b0">macro_context.py → market_agent.py → SCAN/REDUCE/STANDBY</td>
      </tr>
      <tr style="background:#1a1a2e">
        <td style="padding:6px 10px;color:#5dcaa5;font-weight:700">LAYER 3</td>
        <td style="padding:6px 10px;color:#e0e0e0">Signal Engine</td>
        <td style="padding:6px 10px;color:#a0a0b0">universe_builder → signals.py → 16 factors, z-score, Kelly</td>
      </tr>
      <tr>
        <td style="padding:6px 10px;color:#378add;font-weight:700">LAYER 2</td>
        <td style="padding:6px 10px;color:#e0e0e0">Entry Execution</td>
        <td style="padding:6px 10px;color:#a0a0b0">main.py → margin_guard → EntryAgent → BUY orders → ledger</td>
      </tr>
      <tr style="background:#1a1a2e">
        <td style="padding:6px 10px;color:#d85a30;font-weight:700">LAYER 1</td>
        <td style="padding:6px 10px;color:#e0e0e0">Exit Execution</td>
        <td style="padding:6px 10px;color:#a0a0b0">exit_monitor → 6 mechanical exits + math trim + advisory agent</td>
      </tr>
      <tr>
        <td style="padding:6px 10px;color:#639922;font-weight:700">LAYER 0</td>
        <td style="padding:6px 10px;color:#e0e0e0">Position Health</td>
        <td style="padding:6px 10px;color:#a0a0b0">hold_monitor → 8-layer scoring → compute_trim() → hold_health.json</td>
      </tr>
      <tr style="background:#1a1a2e">
        <td style="padding:6px 10px;color:#ba7517;font-weight:700">LEARNING</td>
        <td style="padding:6px 10px;color:#e0e0e0">Outcome Collection</td>
        <td style="padding:6px 10px;color:#a0a0b0">outcome_log + trim_log → prompt_calibrator [PLANNED, needs 30+ trades]</td>
      </tr>
    </table>
  </div>

  <!-- Decision Tree -->
  <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px">Daily Decision Tree</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'Courier New',monospace">
      <tr><td style="padding:3px 10px;color:#6a6a8a">9:00 AM</td><td style="padding:3px 10px;color:#9b8fd4">macro_context.py → macro_context.json (RISK_ON / NEUTRAL / CRISIS)</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">9:15 AM</td><td style="padding:3px 10px;color:#9b8fd4">market_agent.py → market_decision.json (SCAN / REDUCE / STANDBY)</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">9:35 AM</td><td style="padding:3px 10px;color:#378add">main.py → MarginGuard → EntryAgent → BUY orders → ledger.record_entry()</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">9:52 AM</td><td style="padding:3px 10px;color:#d85a30">exit_monitor (mechanical) + hold_monitor (8-layer) + HoldAgent (advisory)</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">3:50 PM</td><td style="padding:3px 10px;color:#d85a30">exit_monitor + hold_monitor + daily_recap email</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">4:30 PM</td><td style="padding:3px 10px;color:#639922">daily_recap.py → email at closing prices</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">Ongoing</td><td style="padding:3px 10px;color:#ba7517">outcome_tracker → outcome_log.json + trim_log.json (calibration data)</td></tr>
      <tr><td style="padding:3px 10px;color:#6a6a8a">Sunday</td><td style="padding:3px 10px;color:#ba7517">[PLANNED] prompt_calibrator.py → rewrites prompts from trade evidence</td></tr>
    </table>
  </div>

  <!-- File Ownership -->
  <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px">JSON State File Ownership</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="border-bottom:1px solid #2a2a3e">
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">File</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Written By</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Read By</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Growth</th>
      </tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #1a1a2e">macro_context.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">macro_context.py</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">market_agent, agent_layer</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #1a1a2e">Overwritten daily</td></tr>
      <tr style="background:#1a1a2e"><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #2a2a3e">market_decision.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">market_agent.py</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">main.py</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #2a2a3e">Overwritten daily</td></tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #1a1a2e">hold_health.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">hold_monitor.py</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">exit_monitor, daily_recap</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #1a1a2e">Overwritten each run</td></tr>
      <tr style="background:#1a1a2e"><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #2a2a3e">hold_history.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">hold_monitor.py</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">hold_monitor (trajectory)</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #2a2a3e">Append-only forever</td></tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #1a1a2e">hold_decisions.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">hold_monitor (HoldAgent)</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">exit_monitor [advisory only]</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #1a1a2e">Append-only forever</td></tr>
      <tr style="background:#1a1a2e"><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #2a2a3e">entry_vetoes.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">main.py (EntryAgent)</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">outcome_tracker</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #2a2a3e">Append-only forever</td></tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #1a1a2e">position_ledger.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">main.py + exit_monitor</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">exit_monitor, hold_monitor, recap</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #1a1a2e">Grows with trades</td></tr>
      <tr style="background:#1a1a2e"><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #2a2a3e">outcome_log.json</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">outcome_tracker.py</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">prompt_calibrator [planned]</td><td style="padding:5px 10px;color:#6a6a8a;border-bottom:1px solid #2a2a3e">Append-only forever</td></tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0">trim_log.json</td><td style="padding:5px 10px;color:#a0a0b0">exit_monitor.py</td><td style="padding:5px 10px;color:#a0a0b0">prompt_calibrator [planned]</td><td style="padding:5px 10px;color:#6a6a8a">Append-only forever</td></tr>
    </table>
  </div>

  <!-- Agent status -->
  <div style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px">Agent Layer Status</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="border-bottom:1px solid #2a2a3e">
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Agent</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Executes</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Role</th>
      </tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #1a1a2e">MarketAgent</td><td style="padding:5px 10px;color:#00d4aa;border-bottom:1px solid #1a1a2e">✓ Rule-based SCAN/REDUCE/STANDBY</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #1a1a2e">LLM adds reasoning only</td></tr>
      <tr style="background:#1a1a2e"><td style="padding:5px 10px;color:#e0e0e0;border-bottom:1px solid #2a2a3e">EntryAgent</td><td style="padding:5px 10px;color:#00d4aa;border-bottom:1px solid #2a2a3e">✓ VETO blocks order</td><td style="padding:5px 10px;color:#a0a0b0;border-bottom:1px solid #2a2a3e">6 structural veto rules</td></tr>
      <tr><td style="padding:5px 10px;color:#e0e0e0">HoldAgent</td><td style="padding:5px 10px;color:#ffa502">Advisory only (math trim executes)</td><td style="padding:5px 10px;color:#a0a0b0">Calibration data collection for Layer 3</td></tr>
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:16px 28px;text-align:center">
    <div style="color:#3a3a5e;font-size:11px">RAPTOR v5.4 | llama3.2 Agent Layer | Math-First Architecture</div>
    <div style="color:#3a3a5e;font-size:10px;margin-top:4px">Paper Trading — Not Financial Advice</div>
  </div>

</div>
</body>
</html>
"""

msg = MIMEMultipart()
msg["From"]    = EMAIL_SENDER
msg["To"]      = EMAIL_RECEIVER
msg["Subject"] = f"RAPTOR v5.4 — System Architecture Ontology ({datetime.now().strftime('%Y-%m-%d')})"
msg.attach(MIMEText(html, "html"))

try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.send_message(msg)
    print("Ontology email sent.")
except Exception as e:
    print(f"Email failed: {e}")

