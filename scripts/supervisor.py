#!/usr/bin/env python3
"""
anti-suicide supervisor for OpenClaw

Sub-commands:
  snapshot     -- capture health baseline + backup files, print session dir
  verify       -- monitor health post-modification, auto-rollback if degraded
  validate-json -- validate a JSON string before writing
  watch        -- continuously watch files for manual edits, auto-rollback on degradation
  rollback     -- manually restore a session or single backup file

Usage:
  SESSION=$(python supervisor.py snapshot --files ~/.openclaw/openclaw.json)
  python supervisor.py verify --session $SESSION --timeout 60 --interval 5
  python supervisor.py validate-json --content '{"key": "value"}'
  python supervisor.py watch --files ~/.openclaw/openclaw.json --interval 3
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


# ── Constants ──────────────────────────────────────────────────────────────────

GATEWAY_PORT = 18789
GATEWAY_API  = f"http://localhost:{GATEWAY_PORT}"
SESSION_BASE = Path(tempfile.gettempdir())

# Minimum channels that should be connected for the service to be considered healthy.
# Set to 0 to only require the gateway itself to be alive.
MIN_HEALTHY_CHANNELS = 0

# External host used to test outbound TCP connectivity (port 443).
# Override with ANTI_SUICIDE_CONNECTIVITY_HOST env var.
CONNECTIVITY_HOST = os.environ.get("ANTI_SUICIDE_CONNECTIVITY_HOST", "8.8.8.8")
CONNECTIVITY_PORT = 443


# ── System config file discovery ───────────────────────────────────────────────

def get_system_config_files() -> list[Path]:
    """
    Return a platform-aware list of system-level config files that can affect
    network connectivity, DNS resolution, firewall rules, and proxy settings.
    Only paths that actually exist on this machine are included.
    """
    import platform
    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        etc    = windir / "System32" / "drivers" / "etc"
        candidates += [
            etc / "hosts",      # DNS override — most common sabotage vector
            etc / "networks",
        ]
        # Shell proxy env vars live in user profile scripts (Git Bash / WSL)
        home = Path.home()
        candidates += [
            home / ".bashrc",
            home / ".bash_profile",
            home / ".profile",
            home / ".gitconfig",    # git http.proxy / https.proxy
            home / ".curlrc",       # curl proxy settings
        ]

    elif system in ("Linux", "Darwin"):
        candidates += [
            Path("/etc/hosts"),
            Path("/etc/resolv.conf"),       # DNS resolver
            Path("/etc/nsswitch.conf"),     # resolver order
            Path("/etc/environment"),       # system-wide env (includes proxy vars)
            Path("/etc/profile"),
            Path("/etc/gitconfig"),
            Path(os.path.expanduser("~/.gitconfig")),
            Path(os.path.expanduser("~/.curlrc")),
            Path(os.path.expanduser("~/.bashrc")),
            Path(os.path.expanduser("~/.bash_profile")),
            Path(os.path.expanduser("~/.profile")),
        ]
        # Firewall
        candidates += [
            Path("/etc/iptables/rules.v4"),
            Path("/etc/iptables/rules.v6"),
            Path("/etc/ufw/ufw.conf"),
            Path("/etc/ufw/user.rules"),
            Path("/etc/firewalld/firewalld.conf"),
        ]
        # Network / proxy
        candidates += [
            Path("/etc/network/interfaces"),
            Path("/etc/systemd/resolved.conf"),
            Path("/etc/proxychains.conf"),
            Path("/etc/proxychains4.conf"),
        ]
        # netplan directory: watch all yaml files inside
        netplan_dir = Path("/etc/netplan")
        if netplan_dir.is_dir():
            candidates += list(netplan_dir.glob("*.yaml"))

        if system == "Darwin":
            candidates += [
                Path("/Library/Preferences/com.apple.alf.plist"),  # macOS firewall
            ]

    return [p for p in candidates if p.exists()]


# ── Health probes ───────────────────────────────────────────────────────────────

def probe_doctor() -> tuple[bool, str]:
    """Run `openclaw doctor` and return (ok, output)."""
    try:
        result = subprocess.run(
            ["openclaw", "doctor"],
            capture_output=True, text=True, timeout=15
        )
        ok = result.returncode == 0
        output = (result.stdout + result.stderr).strip()
        return ok, output
    except FileNotFoundError:
        return False, "openclaw CLI not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "openclaw doctor timed out after 15s"
    except Exception as e:
        return False, f"openclaw doctor failed: {e}"


def probe_gateway_port() -> tuple[bool, str]:
    """Check whether the gateway is listening on its port."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=3):
            return True, f"gateway port {GATEWAY_PORT} is open"
    except (ConnectionRefusedError, OSError) as e:
        return False, f"gateway port {GATEWAY_PORT} not reachable: {e}"


def probe_channels() -> tuple[bool, str, list[dict]]:
    """
    Query the gateway HTTP API for channel status.
    Returns (all_ok, summary_message, list_of_channel_dicts).
    Falls back gracefully if the endpoint is unavailable.
    """
    try:
        import urllib.request
        import urllib.error
        url = f"{GATEWAY_API}/api/v1/channels/status"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # HTTP error from gateway means it's reachable but unhealthy (e.g. 503)
        return False, f"channel API returned HTTP {e.code}: {e.reason}", []
    except Exception as e:
        # Connection refused / timeout — gateway likely not running; skip channel check
        # (gateway probe will already catch this case)
        return True, f"channel API not reachable ({type(e).__name__}), skipping channel check", []

    channels = data.get("channels", [])
    if not channels:
        return True, "no channels configured", []

    unhealthy = [c for c in channels if c.get("status") not in ("connected", "ok", "healthy")]
    healthy_count = len(channels) - len(unhealthy)

    if len(unhealthy) == 0:
        return True, f"all {len(channels)} channels healthy", channels

    summary = (
        f"{healthy_count}/{len(channels)} channels healthy; "
        f"degraded: {[c.get('name', '?') for c in unhealthy]}"
    )

    # Fail only if we fall below the minimum threshold
    ok = healthy_count >= MIN_HEALTHY_CHANNELS
    return ok, summary, channels


def probe_outbound() -> tuple[bool, str]:
    """
    Test outbound TCP connectivity to CONNECTIVITY_HOST:CONNECTIVITY_PORT.
    Catches firewall blocks and broken routing that don't affect the local gateway.
    """
    import socket
    try:
        with socket.create_connection((CONNECTIVITY_HOST, CONNECTIVITY_PORT), timeout=5):
            return True, f"outbound TCP {CONNECTIVITY_HOST}:{CONNECTIVITY_PORT} reachable"
    except (ConnectionRefusedError, OSError) as e:
        return False, f"outbound TCP {CONNECTIVITY_HOST}:{CONNECTIVITY_PORT} blocked: {e}"


def take_health_snapshot() -> dict:
    """Collect a full health snapshot and return it as a dict."""
    doctor_ok, doctor_out   = probe_doctor()
    port_ok,   port_msg     = probe_gateway_port()
    chan_ok,   chan_msg, _   = probe_channels()
    out_ok,    out_msg      = probe_outbound()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "doctor":   {"ok": doctor_ok, "output": doctor_out},
        "gateway":  {"ok": port_ok,   "message": port_msg},
        "channels": {"ok": chan_ok,   "message": chan_msg},
        "outbound": {"ok": out_ok,    "message": out_msg},
        "overall_ok": doctor_ok and port_ok and chan_ok and out_ok,
    }


def is_healthy(snapshot: dict) -> bool:
    return snapshot.get("overall_ok", False)


def print_snapshot(snapshot: dict, label: str = ""):
    ts = snapshot["timestamp"]
    overall = "HEALTHY" if snapshot["overall_ok"] else "DEGRADED"
    prefix = f"[{label}] " if label else ""
    print(f"{prefix}[{ts}] {overall}", file=sys.stderr)
    print(f"  doctor:   {'OK' if snapshot['doctor']['ok'] else 'FAIL'}  — {snapshot['doctor']['output'][:120]}", file=sys.stderr)
    print(f"  gateway:  {'OK' if snapshot['gateway']['ok'] else 'FAIL'}  — {snapshot['gateway']['message']}", file=sys.stderr)
    print(f"  channels: {'OK' if snapshot['channels']['ok'] else 'FAIL'}  — {snapshot['channels']['message']}", file=sys.stderr)
    if "outbound" in snapshot:
        print(f"  outbound: {'OK' if snapshot['outbound']['ok'] else 'FAIL'}  — {snapshot['outbound']['message']}", file=sys.stderr)


# ── Session helpers ────────────────────────────────────────────────────────────

def create_session() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_dir = SESSION_BASE / f"anti-suicide-{ts}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def backup_files(session_dir: Path, file_paths: list[str]) -> list[dict]:
    """Copy each file into session_dir/backups/ and return manifest."""
    backups_dir = session_dir / "backups"
    backups_dir.mkdir(exist_ok=True)
    manifest = []
    for src_str in file_paths:
        src = Path(src_str).expanduser().resolve()
        if not src.exists():
            print(f"  [WARN] file not found, skipping backup: {src}", file=sys.stderr)
            continue
        # Preserve path structure to avoid name collisions
        safe_name = str(src).replace("/", "__").replace("\\", "__").replace(":", "")
        dst = backups_dir / safe_name
        shutil.copy2(src, dst)
        manifest.append({"original": str(src), "backup": str(dst)})
        print(f"  backed up: {src} → {dst}", file=sys.stderr)
    return manifest


def save_session_meta(session_dir: Path, baseline: dict, manifest: list[dict]):
    meta = {
        "session_dir": str(session_dir),
        "baseline": baseline,
        "backups": manifest,
    }
    with open(session_dir / "session.json", "w") as f:
        json.dump(meta, f, indent=2)


def load_session_meta(session_dir: Path) -> dict:
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        print(f"ERROR: session file not found: {meta_path}", file=sys.stderr)
        sys.exit(1)
    with open(meta_path) as f:
        return json.load(f)


# ── Rollback & restart ─────────────────────────────────────────────────────────

def restore_backups(manifest: list[dict]) -> list[str]:
    """Restore all backed-up files to their original paths."""
    restored = []
    for entry in manifest:
        src = Path(entry["backup"])
        dst = Path(entry["original"])
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(str(dst))
            print(f"  restored: {dst}", file=sys.stderr)
        except Exception as e:
            print(f"  [ERROR] could not restore {dst}: {e}", file=sys.stderr)
    return restored


def restart_gateway() -> tuple[bool, str]:
    """Attempt to restart the OpenClaw gateway."""
    # Try the CLI restart command first
    for cmd in [
        ["openclaw", "gateway", "restart"],
        ["openclaw", "restart"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                return True, f"restarted via `{' '.join(cmd)}`"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # Fall back: find and signal the gateway process
    try:
        result = subprocess.run(
            ["pgrep", "-f", "openclaw.*gateway"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        if pids:
            for pid in pids:
                subprocess.run(["kill", "-HUP", pid], check=True)
            return True, f"sent SIGHUP to gateway PIDs: {pids}"
    except Exception:
        pass

    return False, "could not restart gateway automatically — please restart manually"


def do_rollback_and_restart(session_dir: Path, meta: dict, reason: str):
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"AUTO-ROLLBACK TRIGGERED", file=sys.stderr)
    print(f"Reason: {reason}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    restored = restore_backups(meta["backups"])

    if not restored:
        print("  [WARN] No files were backed up — nothing to restore.", file=sys.stderr)
    else:
        print(f"  Restored {len(restored)} file(s).", file=sys.stderr)

    print("  Restarting gateway...", file=sys.stderr)
    ok, msg = restart_gateway()
    print(f"  Gateway restart: {'OK' if ok else 'FAIL'} — {msg}", file=sys.stderr)

    # Wait for gateway to come back up
    if ok:
        print("  Waiting for gateway to become healthy (up to 30s)...", file=sys.stderr)
        for _ in range(6):
            time.sleep(5)
            snap = take_health_snapshot()
            if is_healthy(snap):
                print("  Service recovered successfully after rollback.", file=sys.stderr)
                print_snapshot(snap, label="POST-ROLLBACK")
                break
        else:
            print("  [WARN] Service still not fully healthy after rollback + restart.", file=sys.stderr)
            snap = take_health_snapshot()
            print_snapshot(snap, label="POST-ROLLBACK")

    rollback_log = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "restored_files": restored,
        "gateway_restart": {"ok": ok, "message": msg},
    }
    with open(session_dir / "rollback.json", "w") as f:
        json.dump(rollback_log, f, indent=2)

    print(f"\nRollback log saved to: {session_dir / 'rollback.json'}", file=sys.stderr)
    print("ROLLBACK-COMPLETE", flush=True)


# ── Sub-commands ───────────────────────────────────────────────────────────────

def cmd_snapshot(args):
    """Capture baseline health + backup files. Print session dir path."""
    session_dir = create_session()
    print(f"[anti-suicide] Session: {session_dir}", file=sys.stderr)

    print("[anti-suicide] Capturing health baseline...", file=sys.stderr)
    baseline = take_health_snapshot()
    print_snapshot(baseline, label="BASELINE")

    if not is_healthy(baseline):
        print("\n[anti-suicide] WARNING: Service is ALREADY unhealthy before modification!", file=sys.stderr)
        print("[anti-suicide] Recommended action: fix existing issues before proceeding.", file=sys.stderr)
        # Still create session so caller can choose to proceed with awareness
        print("UNHEALTHY-BASELINE", file=sys.stderr)

    files = list(args.files or [])
    if getattr(args, "system", False):
        sys_files = get_system_config_files()
        files += [str(f) for f in sys_files]
        print(f"[anti-suicide] --system: adding {len(sys_files)} system config file(s):", file=sys.stderr)
        for f in sys_files:
            print(f"  {f}", file=sys.stderr)
    manifest = backup_files(session_dir, files)
    save_session_meta(session_dir, baseline, manifest)

    # Print session path to stdout so caller can capture it
    print(str(session_dir))


def cmd_verify(args):
    """Monitor health after modification. Auto-rollback if degraded."""
    session_dir = Path(args.session)
    meta = load_session_meta(session_dir)
    baseline = meta["baseline"]

    timeout    = args.timeout    # seconds to monitor
    interval   = args.interval   # seconds between checks
    pre_delay  = args.pre_delay  # seconds to wait before first check

    if pre_delay > 0:
        print(f"[anti-suicide] Waiting {pre_delay}s for modification to complete...", file=sys.stderr)
        time.sleep(pre_delay)

    print(f"[anti-suicide] Monitoring health for {timeout}s (interval {interval}s)...", file=sys.stderr)
    print_snapshot(baseline, label="BASELINE (before modification)")

    deadline = time.time() + timeout
    check_num = 0

    while time.time() < deadline:
        time.sleep(interval)
        check_num += 1
        snap = take_health_snapshot()

        elapsed = int(time.time() - (deadline - timeout))
        print(f"\n[anti-suicide] Check #{check_num} at +{elapsed}s:", file=sys.stderr)
        print_snapshot(snap)

        if not is_healthy(snap):
            # Determine which probe failed to form a clear reason
            reasons = []
            if not snap["doctor"]["ok"]:
                reasons.append(f"openclaw doctor: {snap['doctor']['output'][:200]}")
            if not snap["gateway"]["ok"]:
                reasons.append(f"gateway: {snap['gateway']['message']}")
            if not snap["channels"]["ok"]:
                reasons.append(f"channels: {snap['channels']['message']}")
            if not snap.get("outbound", {}).get("ok", True):
                reasons.append(f"outbound: {snap['outbound']['message']}")
            reason = "; ".join(reasons) if reasons else "unknown degradation"

            do_rollback_and_restart(session_dir, meta, reason)
            sys.exit(2)  # Exit 2 = rolled back

    print(f"\n[anti-suicide] Monitoring complete. Service remained healthy for {timeout}s.", file=sys.stderr)
    print("HEALTHY")
    sys.exit(0)


def cmd_validate_json(args):
    """Validate a JSON string. Exit 0 if valid, 1 if invalid."""
    content = args.content
    try:
        parsed = json.loads(content)
        print(f"JSON OK ({type(parsed).__name__})")
        sys.exit(0)
    except json.JSONDecodeError as e:
        print(f"INVALID JSON: {e}")
        sys.exit(1)


def _file_hash(p: Path) -> str | None:
    """Return MD5 hex digest of file contents, or None if unreadable."""
    try:
        h = hashlib.md5()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return None


def cmd_watch(args):
    """
    Continuously poll config files for manual changes.
    On change: monitor health for --verify-timeout seconds.
    If health degrades: auto-rollback to the last known-good state.
    If health holds: advance the safe backup to the new state.
    """
    explicit = [Path(f).expanduser().resolve() for f in (args.files or [])]
    system   = get_system_config_files() if getattr(args, "system", False) else []
    # Deduplicate while preserving order: explicit files first, then system files
    seen: set[Path] = set()
    files: list[Path] = []
    for f in explicit + system:
        if f not in seen:
            seen.add(f)
            files.append(f)

    if not files:
        print("ERROR: no files to watch. Provide --files and/or --system.", file=sys.stderr)
        sys.exit(1)

    poll_interval    = args.interval
    verify_timeout   = args.verify_timeout
    verify_interval  = args.verify_interval

    print(f"[anti-suicide] watch mode — monitoring {len(files)} file(s)", file=sys.stderr)
    if system:
        print(f"  ({len(explicit)} explicit + {len(system)} system)", file=sys.stderr)
    for f in files:
        print(f"  {f}", file=sys.stderr)

    # ── Initial safe backup ────────────────────────────────────────────────────
    safe_session = create_session()
    print(f"[anti-suicide] Initial session: {safe_session}", file=sys.stderr)

    baseline = take_health_snapshot()
    print_snapshot(baseline, label="BASELINE")
    if not is_healthy(baseline):
        print("[anti-suicide] WARNING: Service already unhealthy. Watching anyway.", file=sys.stderr)

    manifest = backup_files(safe_session, [str(f) for f in files])
    save_session_meta(safe_session, baseline, manifest)
    safe_meta = load_session_meta(safe_session)

    # Record initial file hashes (known-good state)
    file_hashes: dict[Path, str | None] = {f: _file_hash(f) for f in files}

    print(f"[anti-suicide] Polling every {poll_interval}s. Press Ctrl+C to stop.", file=sys.stderr)

    try:
        while True:
            time.sleep(poll_interval)

            # ── Detect changes ─────────────────────────────────────────────────
            changed: list[Path] = []
            for f in files:
                new_hash = _file_hash(f)
                if new_hash != file_hashes[f]:
                    changed.append(f)
                    file_hashes[f] = new_hash

            if not changed:
                continue

            ts = datetime.now(timezone.utc).isoformat()
            print(f"\n[anti-suicide] [{ts}] Manual change detected:", file=sys.stderr)
            for f in changed:
                print(f"  CHANGED: {f}", file=sys.stderr)

            # ── Post-change health monitoring ──────────────────────────────────
            print(f"[anti-suicide] Monitoring health for {verify_timeout}s "
                  f"(interval {verify_interval}s)...", file=sys.stderr)

            deadline = time.time() + verify_timeout
            check_num = 0
            rolled_back = False

            while time.time() < deadline:
                time.sleep(verify_interval)
                check_num += 1
                snap = take_health_snapshot()
                elapsed = int(time.time() - (deadline - verify_timeout))
                print(f"\n[anti-suicide] Check #{check_num} at +{elapsed}s:", file=sys.stderr)
                print_snapshot(snap)

                if not is_healthy(snap):
                    reasons = []
                    if not snap["doctor"]["ok"]:
                        reasons.append(f"openclaw doctor: {snap['doctor']['output'][:200]}")
                    if not snap["gateway"]["ok"]:
                        reasons.append(f"gateway: {snap['gateway']['message']}")
                    if not snap["channels"]["ok"]:
                        reasons.append(f"channels: {snap['channels']['message']}")
                    if not snap.get("outbound", {}).get("ok", True):
                        reasons.append(f"outbound: {snap['outbound']['message']}")
                    reason = "; ".join(reasons) if reasons else "unknown degradation"

                    do_rollback_and_restart(safe_session, safe_meta, reason)

                    # Reset hashes to match the restored files
                    file_hashes = {f: _file_hash(f) for f in files}
                    rolled_back = True
                    break

            if not rolled_back:
                # ── Advance safe backup to new state ───────────────────────────
                print(f"\n[anti-suicide] Service healthy after change. Advancing safe backup.",
                      file=sys.stderr)
                safe_session = create_session()
                manifest = backup_files(safe_session, [str(f) for f in files])
                new_baseline = take_health_snapshot()
                save_session_meta(safe_session, new_baseline, manifest)
                safe_meta = load_session_meta(safe_session)
                print(f"[anti-suicide] New safe session: {safe_session}", file=sys.stderr)

    except KeyboardInterrupt:
        print("\n[anti-suicide] Watch mode stopped.", file=sys.stderr)


def cmd_rollback(args):
    """Manual rollback of a session or a single file."""
    if args.session:
        session_dir = Path(args.session)
        meta = load_session_meta(session_dir)
        do_rollback_and_restart(session_dir, meta, reason="manual rollback requested by user")
    elif args.file:
        backup_path = Path(args.file)
        if not backup_path.exists():
            print(f"ERROR: backup file not found: {backup_path}", file=sys.stderr)
            sys.exit(1)
        # Derive original path from the safe_name encoding
        safe_name = backup_path.name
        original = safe_name.replace("__", "/")
        if original.startswith("/"):
            pass  # Linux-style absolute path
        else:
            # Windows-style: first segment is drive letter
            original = original[:1] + ":" + original[1:]
        print(f"  Restoring {backup_path} → {original}", file=sys.stderr)
        shutil.copy2(backup_path, original)
        print("RESTORED")
    else:
        print("ERROR: provide --session or --file", file=sys.stderr)
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="anti-suicide supervisor for OpenClaw")
    sub = parser.add_subparsers(dest="command", required=True)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture baseline + backup files")
    p_snap.add_argument("--files", nargs="*", default=[], metavar="FILE",
                        help="Files that will be modified (to back up)")
    p_snap.add_argument("--system", action="store_true",
                        help="Also back up auto-detected system config files (hosts, resolv.conf, etc.)")

    # verify
    p_verify = sub.add_parser("verify", help="Monitor health post-modification")
    p_verify.add_argument("--session", required=True, metavar="SESSION_DIR",
                          help="Session directory returned by snapshot")
    p_verify.add_argument("--timeout",    type=int, default=60,
                          help="Seconds to monitor (default: 60)")
    p_verify.add_argument("--interval",   type=int, default=5,
                          help="Seconds between health checks (default: 5)")
    p_verify.add_argument("--pre-delay",  type=int, default=0, dest="pre_delay",
                          help="Seconds to wait before first check, for plugin-spawned background use (default: 0)")

    # validate-json
    p_vj = sub.add_parser("validate-json", help="Validate a JSON string")
    p_vj.add_argument("--content", required=True, help="JSON string to validate")

    # watch
    p_watch = sub.add_parser("watch", help="Watch files for manual edits; auto-rollback on health degradation")
    p_watch.add_argument("--files", nargs="*", default=[], metavar="FILE",
                         help="Config files to watch (can be combined with --system)")
    p_watch.add_argument("--system", action="store_true",
                         help="Also watch auto-detected system config files (hosts, proxy env files, etc.)")
    p_watch.add_argument("--interval",        type=int, default=3,
                         help="Seconds between file-state polls (default: 3)")
    p_watch.add_argument("--verify-timeout",  type=int, default=60, dest="verify_timeout",
                         help="Seconds to monitor health after a change is detected (default: 60)")
    p_watch.add_argument("--verify-interval", type=int, default=5,  dest="verify_interval",
                         help="Seconds between health checks during verify window (default: 5)")

    # rollback
    p_rb = sub.add_parser("rollback", help="Manual rollback")
    rb_group = p_rb.add_mutually_exclusive_group(required=True)
    rb_group.add_argument("--session", metavar="SESSION_DIR",
                          help="Session directory to fully restore")
    rb_group.add_argument("--file", metavar="BACKUP_FILE",
                          help="Single backup file to restore")

    args = parser.parse_args()

    dispatch = {
        "snapshot":      cmd_snapshot,
        "verify":        cmd_verify,
        "validate-json": cmd_validate_json,
        "watch":         cmd_watch,
        "rollback":      cmd_rollback,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
