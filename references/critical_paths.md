# OpenClaw Critical Paths Reference

## Configuration Files

### `~/.openclaw/openclaw.json`
Primary configuration. Gateway reads this at startup.

**Required top-level keys:**
- `agent` — model selection and agent defaults
- Any channel credentials/tokens

**Danger:** removing or corrupting this file = gateway won't start.

---

### `~/.openclaw/.env`
Environment variables for API keys and channel tokens.

**Common critical variables:**
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — model access
- `TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN`, `DISCORD_BOT_TOKEN` — channel auth
- `OPENCLAW_GATEWAY_PORT` — defaults to 18789

**Danger:** deleting a variable silently breaks only that channel; the service starts but the channel never connects.

---

## Workspace Prompt Files

These files live in `~/.openclaw/workspace/` and are injected into every agent session.

### `AGENTS.md`
Defines agent identity, capabilities, and behavioral rules.

**Danger:** overwriting with empty content or wrong instructions changes how the agent responds to everything.

### `SOUL.md`
Core personality, communication style, and values.

**Danger:** same as AGENTS.md — subtle corruption is worse than obvious corruption because it's hard to detect.

### `TOOLS.md`
Documents which tools the agent can use and how.

**Danger:** incorrect tool descriptions cause the agent to misuse or refuse to use available tools.

---

## Skills

### `~/.openclaw/workspace/skills/<name>/SKILL.md`
Individual skill definitions. Corrupting one skill only breaks that skill, not the gateway, but it can cause unexpected agent behavior if the skill is frequently triggered.

---

## Service Files

### `docker-compose.yml` (if using Docker deployment)
**Danger:** wrong port mappings or missing environment variable mappings = container starts but is unreachable.

### `package.json` / `pnpm-workspace.yaml`
**Danger:** changing dependency versions or workspace patterns breaks `pnpm install` which is required for updates.

---

## Gateway HTTP API (health check endpoints)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/channels/status` | GET | Per-channel connection state |
| `/api/v1/health` | GET | Overall gateway health |

Default base URL: `http://localhost:18789`

Channel status values: `connected`, `disconnected`, `error`, `reconnecting`

---

## Recovery Commands

```bash
# Check overall health
openclaw doctor

# Restart gateway
openclaw gateway restart

# Check what version is running
openclaw update status

# View active sessions/channels
openclaw nodes
```
