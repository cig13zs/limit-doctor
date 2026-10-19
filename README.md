# limit-doctor

[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-ff5e5b?logo=ko-fi&logoColor=white)](https://ko-fi.com/jju1s)

limit-doctor is a lightweight, zero-dependency CLI that analyzes your local Claude Code and Codex session logs to diagnose what is draining your token limits. It pinpoints idle cache expirations, oversized contexts, unused MCP servers, and heavy session starts, giving you the single direct fix for each cause.

## Install and Run

Requires Python 3.10 or newer (Python standard library only).

```bash
git clone https://github.com/jju1s/limit-doctor.git
cd limit-doctor
python limit_doctor.py
```

Optional flags:
* `--days N`: analysis window in days (default: 7)
* `--json`: print structured machine-readable JSON
* `--md report.md`: export report to markdown
* `--no-codex`: skip Codex logs
* `--no-claude`: skip Claude logs

## Sample Report

```text
LIMIT DOCTOR v0.1
Window: last 30 days | Cost model: API ratios (read 0.1x, 5m write 1.25x, 1h write 2.0x, out 5.0x)
Percentages are shares of your own usage, not published plan limits.
--------------------------------------------------------------------------------
CLAUDE CODE USAGE: 178,020 weighted tokens across 5 turns in 1 sessions

TOP FINDINGS (ranked by weighted token cost):
1. Cache rebuilds after idle [ 46.1% | 82,050 weighted tokens]
   Count: 2 rebuilds
   Worst sessions:
     - sess-test-1...: 2 rebuilds (82,050 weighted tokens)
   Fix: finish or /clear before stepping away; long breaks re-bill the whole context.

2. Heavy session start [ 24.8% | 44,150 weighted tokens]
   First-turn cache write median: 35,000 [FLAGGED > 30k] | worst: 35,000
   Fix: fewer always-on MCP servers and a shorter CLAUDE.md.

3. Subagents and sidechains [  0.8% | 1,420 weighted tokens]
   Subagent turns: 1
   Models: claude-3-5-haiku-20241022: 1,420
   Fix: limit autonomous subagent depth and review subagent task scope.

4. Long contexts (median context > 100k) [  0.0% | 0 weighted tokens]
   Sessions with median context > 100k: 0 | Turns past 100k: 0
   Fix: start a new session per task.

5. Sessions with 10+ images [  0.0% | 0 weighted tokens]
   Total images: 1 | Sessions with 10+ images: 0
   Fix: crop or downscale screenshots; prefer text reads.

CONFIG & EFFICIENCY FINDINGS:
Unused MCP servers (configured but 0 calls in window):
  * unused_server_user (user) -> claude mcp remove unused_server_user -s user
  * unused_proj_server (project (demo/proj1)) -> claude mcp remove unused_proj_server -s project
  * unused_settings_server (settings) -> remove mcpServers.unused_settings_server in ~/.claude/settings.json
  * unused_plugin (plugin) -> set enabledPlugins.unused_plugin to false in ~/.claude/settings.json
  * unused_codex_server (codex) -> remove [mcp_servers.unused_codex_server] in ~/.codex/config.toml
Model mix:
  * claude-opus-5-5: 176,600 weighted tokens (99.2%)
  * claude-3-5-haiku-20241022: 1,420 weighted tokens (0.8%)
  Hint: most short turns (<200 output tokens) ran on claude-opus-5-5 (80.0%),
  which is the most expensive model.

BREAKDOWN:
Projects (cwd):
  demo/proj1                              5 turns       178,020 tokens (100.0%)
Daily usage:
  2026-10-19       5 turns       178,020 tokens (100.0%)
Top 5 sessions:
  sess-test-1...   2026-10-19  demo/proj1              5 turns     178,020 tokens (100.0%)

CODEX (separate usage, not summed with Claude):
Latest rate limits: 25.0% used (window: 10080m, resets at 2026-10-14 03:47 UTC)
Daily tokens:
  2026-10-19: 890 weighted tokens (cached input share: 40.0%)
```

## What It Reads

limit-doctor operates in read-only mode on local paths:
* `~/.claude/projects/` and `~/.claude/projects/*/subagents/` (Claude session jsonl logs)
* `~/.codex/sessions/` (Codex rollout jsonl logs)
* `~/.claude.json`, `~/.claude/settings.json`, and `.mcp.json` (MCP server registrations)
* `~/.codex/config.toml` (Codex MCP configuration)

It never modifies your logs or configs, and it never prints prompt or response text.

## Privacy

limit-doctor runs 100% locally on your computer. It makes no external network requests, collects no analytics, and sends nothing anywhere.

## License

MIT (c) 2026 jju1s
