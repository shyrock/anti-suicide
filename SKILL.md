---
name: anti-suicide
description: Safety guardrail that prevents OpenClaw from self-destructing via erroneous self-modification. ALWAYS use this skill before modifying any of OpenClaw's own critical files: openclaw.json, workspace prompt files (AGENTS.md, SOUL.md, TOOLS.md), installed skills, .env files, docker-compose.yml, or any file inside ~/.openclaw/ or the openclaw installation directory. Also trigger when the user says "update my config", "modify openclaw settings", "change the agent prompt", "edit my soul/agents/tools file", "reinstall", or any operation that touches OpenClaw's own runtime files. The goal is to enforce Backup → Snapshot → Modify → Verify for every self-modification, with automatic rollback if health degrades.
---

# Anti-Suicide Skill

OpenClaw is an always-on personal assistant. One bad write to `openclaw.json` or a corrupted `SOUL.md` can take it offline or alter its behavior in subtle, hard-to-debug ways. This skill enforces a safe modification protocol using a supervisor script that automatically rolls back changes if the service degrades.

## What Counts as "Self-Modification"

Any write, edit, or delete targeting:

| Path | Risk |
|------|------|
| `~/.openclaw/openclaw.json` | Gateway fails to start; all channels go dark |
| `~/.openclaw/workspace/AGENTS.md` | Agent identity/capabilities silently corrupted |
| `~/.openclaw/workspace/SOUL.md` | Personality and core behavior overwritten |
| `~/.openclaw/workspace/TOOLS.md` | Available tools list broken |
| `~/.openclaw/workspace/skills/*/SKILL.md` | Individual skill broken or lost |
| `~/.openclaw/.env` | API keys or channel tokens lost |
| `docker-compose.yml` | Container won't start |
| `package.json` / `pnpm-workspace.yaml` | Dependency resolution broken |

See `references/critical_paths.md` for field-level details.

---

## Safe Modification Protocol

Every self-modification follows these five steps in strict order.

### Step 1 — Start the Supervisor (Before Touching Anything)

The supervisor captures a health baseline and returns a session ID. Start it **before** making any changes:

```bash
SESSION=$(python ~/.openclaw/workspace/skills/anti-suicide/scripts/supervisor.py snapshot \
  --files <file1> [file2 ...])
echo "Session: $SESSION"
```

The session ID is a path like `/tmp/anti-suicide-<timestamp>/`. Save it — you'll need it in Step 4.

If the supervisor reports the service is already unhealthy at baseline, **stop immediately**. Do not make modifications on a service that is already degraded. Report the health output to the user and ask how to proceed.

### Step 2 — Show the Diff

Before applying any change, show the user exactly what is changing:

```
FILE: <filepath>

BEFORE:
───────────────────
<current content of the affected section>
───────────────────

AFTER:
───────────────────
<proposed content>
───────────────────
```

For JSON files, also validate the proposed content parses correctly before showing the diff:
```bash
python ~/.openclaw/workspace/skills/anti-suicide/scripts/supervisor.py validate-json \
  --content '<proposed_json>'
```

If JSON is invalid, stop here and tell the user what needs fixing.

### Step 3 — Confirm

Say exactly:

> "About to modify `<filepath>`. Supervisor session `<SESSION>` is active and will auto-rollback if the service degrades. The diff is shown above. Shall I proceed?"

Wait for explicit confirmation before writing anything.

### Step 4 — Apply the Change

Make the modification using the Edit tool (never Write for existing files, to avoid accidental full overwrites).

### Step 5 — Verify (Supervisor Monitors and Auto-Heals)

After applying, hand off to the supervisor to monitor the service:

```bash
python ~/.openclaw/workspace/skills/anti-suicide/scripts/supervisor.py verify \
  --session $SESSION \
  --timeout 60 \
  --interval 5
```

The supervisor will:
1. Poll `openclaw doctor`, gateway liveness, and channel status every 5 seconds for 60 seconds
2. If health is stable throughout → print `HEALTHY` and exit 0
3. If health degrades → automatically rollback all backed-up files, restart the gateway, and print a report

Report the result to the user. If auto-rollback occurred, say what was rolled back and that the service has been restored.

---

## Hard Blocks

The following are never safe without explicit user override:

- **Deleting** `openclaw.json`, `SOUL.md`, `AGENTS.md`, or `TOOLS.md` entirely
- **Overwriting** a prompt file with empty or near-empty content
- **Killing the gateway process** without first warning the user about downtime
- **Running `npm install` or `pnpm install`** inside `~/.openclaw/` without confirming no pinned versions will break
- **Running `docker-compose down`** without warning that all channels will disconnect

---

## Manual Rollback

If you need to rollback outside of the supervisor flow:

```bash
python ~/.openclaw/workspace/skills/anti-suicide/scripts/rollback.py --session <SESSION>
openclaw gateway restart
```

Or restore a specific file:
```bash
python ~/.openclaw/workspace/skills/anti-suicide/scripts/rollback.py --file <backup_file_path>
```

---

## References

- `references/critical_paths.md` — full list of critical file paths with required structure
