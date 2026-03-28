/**
 * anti-suicide plugin for OpenClaw
 *
 * Automatically intercepts any Write/Edit/MultiEdit tool call targeting
 * critical OpenClaw config files, runs supervisor.py snapshot (backup),
 * and spawns a background verify process that auto-rollbacks if health
 * degrades after the modification.
 *
 * Install: copy this directory to ~/.openclaw/extensions/anti-suicide/
 * then run: npm install && npm run build
 * See INSTALL.md for full instructions.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawnSync, spawn } from "child_process";
import * as os from "os";
import * as path from "path";

// ── Critical path patterns ─────────────────────────────────────────────────

const OPENCLAW_HOME = path.join(os.homedir(), ".openclaw");

/** Absolute path prefixes / exact filenames that are considered critical. */
const CRITICAL_PREFIXES: string[] = [
  OPENCLAW_HOME,
];

/** Basename-level matches (regardless of directory). */
const CRITICAL_BASENAMES: string[] = [
  "openclaw.json",
  "AGENTS.md",
  "SOUL.md",
  "TOOLS.md",
  "docker-compose.yml",
  "docker-compose.yaml",
];

const SUPERVISOR_PY = path.join(
  OPENCLAW_HOME,
  "workspace/skills/anti-suicide/scripts/supervisor.py"
);

// ── Helpers ────────────────────────────────────────────────────────────────

function isCriticalPath(filePath: string): boolean {
  if (!filePath) return false;
  const resolved = path.resolve(filePath.replace(/^~/, os.homedir()));
  if (CRITICAL_PREFIXES.some((p) => resolved.startsWith(p))) return true;
  if (CRITICAL_BASENAMES.includes(path.basename(resolved))) return true;
  return false;
}

/** Extract file paths from tool params depending on the tool name. */
function extractFilePaths(toolName: string, params: Record<string, unknown>): string[] {
  switch (toolName) {
    case "Write":
      return params.file_path ? [String(params.file_path)] : [];
    case "Edit":
    case "MultiEdit":
      return params.file_path ? [String(params.file_path)] : [];
    case "Bash": {
      // Heuristic: look for common write patterns targeting critical paths
      const cmd = String(params.command ?? "");
      const matches = [...cmd.matchAll(/(?:>|tee|cp|mv)\s+([^\s;|&]+)/g)];
      return matches
        .map((m) => m[1])
        .filter((f) => isCriticalPath(f));
    }
    default:
      return [];
  }
}

/** Run supervisor.py snapshot synchronously and return the session path. */
function runSnapshot(filePaths: string[]): string | null {
  const args = ["snapshot", "--files", ...filePaths];
  const result = spawnSync("python", [SUPERVISOR_PY, ...args], {
    encoding: "utf-8",
    timeout: 15_000,
  });

  if (result.error) {
    console.error(`[anti-suicide] snapshot failed to start: ${result.error.message}`);
    return null;
  }

  const sessionDir = result.stdout?.trim();
  if (!sessionDir) {
    console.error(`[anti-suicide] snapshot returned no session dir. stderr: ${result.stderr}`);
    return null;
  }

  // Warn but don't block if baseline was already unhealthy
  if (result.stderr?.includes("UNHEALTHY-BASELINE")) {
    console.warn("[anti-suicide] WARNING: Service was already unhealthy before this modification!");
  }

  return sessionDir;
}

/** Spawn supervisor.py verify as a detached background process. */
function spawnVerify(sessionDir: string, preDelay = 5): void {
  const child = spawn(
    "python",
    [
      SUPERVISOR_PY,
      "verify",
      "--session", sessionDir,
      "--timeout",   "60",
      "--interval",  "5",
      "--pre-delay", String(preDelay),
    ],
    {
      detached: true,
      stdio: "ignore",  // fully detached — logs go to rollback.json in session dir
    }
  );
  child.unref();
  console.error(`[anti-suicide] verify process spawned (PID ${child.pid}) for session: ${sessionDir}`);
}

// ── Plugin entry ───────────────────────────────────────────────────────────

export default definePluginEntry({
  id: "anti-suicide",

  register(api) {
    api.on("before_tool_call", (event) => {
      const { toolName, params } = event;

      const filePaths = extractFilePaths(toolName, params as Record<string, unknown>);
      const criticalPaths = filePaths.filter(isCriticalPath);

      if (criticalPaths.length === 0) {
        return; // Not a critical-path modification — pass through
      }

      console.error(
        `[anti-suicide] Intercepted ${toolName} targeting critical path(s): ${criticalPaths.join(", ")}`
      );

      // Step 1: backup + health snapshot (synchronous, must complete before write)
      const sessionDir = runSnapshot(criticalPaths);

      if (!sessionDir) {
        // Supervisor unavailable — warn but don't block the operation
        console.error("[anti-suicide] Could not run snapshot. Proceeding without backup.");
        return;
      }

      // Step 2: spawn background verify (will auto-rollback if health degrades)
      // pre-delay=5 gives the tool call time to complete before monitoring begins
      spawnVerify(sessionDir, 5);

      // Do NOT block the tool call — the backup is taken, monitoring is running.
      // Blocking here would prevent the modification entirely, which is not our goal.
      // Our goal is: backup before + auto-heal after.
    });
  },
});
