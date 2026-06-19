"""
sync_to_claude.py
PURPOSE : Keep your local Raptor repo, GitHub, and the Claude Project in sync.
RUN THIS : Before every Claude session AND after every git push.
LOCATION : C:\\Users\\steve\\OneDrive\\Desktop\\Raptor\\
USAGE    : python sync_to_claude.py
"""

import subprocess
import shutil
import hashlib
import json
from pathlib import Path
from datetime import datetime

RAPTOR_PATH = Path(r"C:\Raptor")

ALWAYS_UPLOAD = [
    "RAPTOR_SKILL.md",
    "RAPTOR_STARTUP.md",
    "RAPTOR_MASTER_PLAN.md",
    "RAPTOR_ONTOLOGY.md",
    "data_feeds.py",
    "signals.py",
    "main.py",
    "exit_monitor.py",
    "agent_layer.py",
    "config.py",
    "position_ledger.json",
    "outcome_log.json",
    "position_outcomes.json",
    "sync_to_claude.py",
]

def run(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or RAPTOR_PATH)
    return result.stdout.strip(), result.stderr.strip()

def md5(filepath):
    try:
        return hashlib.md5(Path(filepath).read_bytes()).hexdigest()[:8]
    except Exception:
        return "ERROR"

def section(title):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")

def ok(msg):   print(f"  [OK]  {msg}")
def warn(msg): print(f"  [!!]  {msg}")
def info(msg): print(f"        {msg}")

# ---------------------------------------------------------------
print(f"\n=== RAPTOR SYNC  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

# STEP 1 - Pull from GitHub
section("1/4  Pull from GitHub")
out, err = run("git pull origin main")
print(f"  {out or err}")
hash_out, _ = run("git log --oneline -1")
ok(f"Latest commit: {hash_out}")

# STEP 2 - Clear __pycache__
section("2/4  Clear __pycache__")
removed = 0
for p in RAPTOR_PATH.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)
    removed += 1
ok(f"Removed {removed} __pycache__ folder(s)")

# STEP 3 - Files changed in last commit
section("3/4  Files changed in last commit")
changed_out, _ = run("git diff --name-only HEAD~1 HEAD")
changed_files = [f.strip() for f in changed_out.splitlines() if f.strip()] if changed_out else []
if changed_files:
    for f in changed_files:
        print(f"  CHANGED: {f}")
else:
    info("(no diff - first or only commit)")

# STEP 4 - Write sync_manifest.json (checksums for Claude to verify)
section("4/4  Writing sync_manifest.json")
manifest = {
    "generated": datetime.now().isoformat(),
    "commit": hash_out,
    "files": {}
}
upload_set = sorted(set(changed_files) | set(ALWAYS_UPLOAD))
for f in upload_set:
    fpath = RAPTOR_PATH / f
    if fpath.exists():
        manifest["files"][f] = {
            "md5": md5(fpath),
            "size": fpath.stat().st_size,
            "modified": datetime.fromtimestamp(fpath.stat().st_mtime).isoformat()
        }
    else:
        manifest["files"][f] = {"status": "MISSING"}

manifest_path = RAPTOR_PATH / "sync_manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2))
ok(f"Written: sync_manifest.json")

# STEP 5 - Print upload list
print()
print("  FILES TO UPLOAD TO CLAUDE PROJECT (include sync_manifest.json):")
for f in upload_set:
    fpath = RAPTOR_PATH / f
    status = "EXISTS" if fpath.exists() else "MISSING"
    chk = md5(fpath) if fpath.exists() else "------"
    print(f"    [{status}]  {f:<40} md5={chk}")

print()
print("=" * 55)
print("  SYNC COMPLETE")
print(f"  Commit hash for Claude: {hash_out}")
print()
print("  HOW TO VERIFY SYNC WITH CLAUDE:")
print("  1. Upload all files above + sync_manifest.json to Claude Project")
print("  2. Tell Claude: 'verify sync' and paste the commit hash")
print("  3. Claude reads sync_manifest.json and confirms checksums match")
print("=" * 55)
print()
