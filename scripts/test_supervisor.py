#!/usr/bin/env python3
"""
End-to-end test for the anti-suicide supervisor.

Tests:
  1. validate-json  -- valid and invalid input
  2. snapshot       -- backs up file, captures baseline
  3. verify OK      -- healthy throughout, exits 0
  4. verify ROLLBACK-- health fails mid-window, file is restored, exits 2
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SUPERVISOR = Path(__file__).parent / "supervisor.py"
PYTHON = sys.executable

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []

def run(*args, input=None):
    return subprocess.run(
        [PYTHON, str(SUPERVISOR), *args],
        capture_output=True, text=True, input=input
    )

def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    results.append(condition)

# ─── Test 1: validate-json ────────────────────────────────────────────────────
print("\n=== Test 1: validate-json ===")

r = run("validate-json", "--content", '{"agent": "claude-opus-4-6", "port": 18789}')
check("valid JSON exits 0",   r.returncode == 0)
check("valid JSON prints OK", "JSON OK" in r.stdout)

r = run("validate-json", "--content", '{bad json here')
check("invalid JSON exits 1",        r.returncode == 1)
check("invalid JSON reports error",  "INVALID JSON" in r.stdout)

# ─── Test 2: snapshot ─────────────────────────────────────────────────────────
print("\n=== Test 2: snapshot (backup) ===")

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
    original_content = '{"agent": "claude-opus-4-6"}'
    f.write(original_content)
    test_file = f.name

r = run("snapshot", "--files", test_file)
check("snapshot exits 0", r.returncode == 0)

session_dir = r.stdout.strip()
check("session dir printed to stdout", bool(session_dir), session_dir)
check("session dir exists",            Path(session_dir).exists())

session_json = Path(session_dir) / "session.json"
check("session.json written", session_json.exists())

meta = json.loads(session_json.read_text())
check("baseline captured",  "baseline" in meta)
check("backup entry exists", len(meta["backups"]) == 1)

backup_path = Path(meta["backups"][0]["backup"])
check("backup file exists",    backup_path.exists())
check("backup content matches", backup_path.read_text() == original_content)

# ─── Test 3: verify — stays healthy (mock openclaw as always-OK) ───────────────
print("\n=== Test 3: verify — healthy path ===")

# Patch supervisor to use a mock health check that always returns healthy.
# We do this by temporarily monkey-patching via env var + a wrapper script.
mock_healthy_script = Path(tempfile.mktemp(suffix="_mock_healthy.py"))
mock_healthy_script.write_text(f"""
import sys, os
sys.path.insert(0, str({str(SUPERVISOR.parent)!r}))
import supervisor

# Override all probes to return healthy
supervisor.probe_doctor   = lambda: (True, "mock: OK")
supervisor.probe_gateway_port = lambda: (True, "mock: port open")
supervisor.probe_channels = lambda: (True, "mock: all channels healthy", [])

sys.argv = ["supervisor.py", "verify",
            "--session", {session_dir!r},
            "--timeout", "8",
            "--interval", "2"]
supervisor.main()
""")

r = subprocess.run([PYTHON, str(mock_healthy_script)], capture_output=True, text=True)
check("verify healthy exits 0",       r.returncode == 0, f"exit={r.returncode}")
check("verify prints HEALTHY",        "HEALTHY" in r.stdout)
mock_healthy_script.unlink()

# ─── Test 4: verify — health fails → auto-rollback ───────────────────────────
print("\n=== Test 4: verify — auto-rollback path ===")

# Modify the test file to simulate a "bad" change
Path(test_file).write_text('{"agent": "CORRUPTED", "broken": true}')
check("test file modified",  Path(test_file).read_text() != original_content)

# Take a new snapshot of the *modified* file (simulating backup-before-change)
# Actually we re-use the existing snapshot from Test 2 which backed up the original.

# Patch probes: first call returns healthy, subsequent calls return unhealthy
mock_fail_script = Path(tempfile.mktemp(suffix="_mock_fail.py"))
mock_fail_script.write_text(f"""
import sys, os
sys.path.insert(0, str({str(SUPERVISOR.parent)!r}))
import supervisor

call_count = 0

def mock_doctor():
    global call_count
    call_count += 1
    if call_count <= 1:
        return True, "mock: first check OK"
    return False, "mock: gateway crashed after config change"

supervisor.probe_doctor       = mock_doctor
supervisor.probe_gateway_port = lambda: (True, "mock: port open")
supervisor.probe_channels     = lambda: (True, "mock: channels ok", [])

# Disable actual gateway restart so test doesn't try to run openclaw
supervisor.restart_gateway = lambda: (True, "mock: restarted")

sys.argv = ["supervisor.py", "verify",
            "--session", {session_dir!r},
            "--timeout", "20",
            "--interval", "2"]
supervisor.main()
""")

r = subprocess.run([PYTHON, str(mock_fail_script)], capture_output=True, text=True)
check("verify rollback exits 2",          r.returncode == 2, f"exit={r.returncode}")
check("rollback triggered message",       "AUTO-ROLLBACK TRIGGERED" in r.stderr)
check("original file restored",          Path(test_file).read_text() == original_content,
      f"content={Path(test_file).read_text()!r}")
check("rollback.json written",           (Path(session_dir) / "rollback.json").exists())
mock_fail_script.unlink()

# ─── Cleanup ──────────────────────────────────────────────────────────────────
os.unlink(test_file)

# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*40}")
passed = sum(results)
total  = len(results)
status = PASS if passed == total else FAIL
print(f"[{status}] {passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
