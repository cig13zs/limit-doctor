# limit-doctor

[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-ff5e5b?logo=ko-fi&logoColor=white)](https://ko-fi.com/jju1s)
[![Tests](https://img.shields.io/github/actions/workflow/status/cig13zs/limit-doctor/test.yml?label=tests)](https://github.com/cig13zs/limit-doctor/actions)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Reads your local Claude Code and Codex logs and tells you where your usage went, ranked by cost, with the fix for each cause.

I wrote it because my weekly limit kept running low and I could not tell why. My own last 7 days:

```text
TOP FINDINGS (ranked by weighted token cost; a turn can count in several):
1. Subagents and sidechains [ 63.9% | 301,893,863 weighted tokens]
   Subagent turns: 9,916
   Fix: cheaper models and tight briefs for mechanical subagent jobs.

2. Context past 100k [ 33.4% | 157,962,809 weighted tokens]
   Sessions with median context > 100k: 35 | Turns past 100k: 13,681
   Fix: start a new session per task.

3. Images kept in context (estimate) [  8.6% | 40,658,730 weighted tokens]
   Total images: 5,286 | Sessions with 10+ images: 26

4. Cache rebuilds after idle [  5.5% | 26,069,887 weighted tokens]
   Count: 91 rebuilds

Unused MCP servers (configured but 0 calls in window):
  * ltspice (project (projects/circuits)) -> claude mcp remove ltspice -s project
```

Two thirds of it was subagents. I had no idea.

## Run it

Python 3.11 or newer, nothing to install.

```bash
git clone https://github.com/cig13zs/limit-doctor
cd limit-doctor
python limit_doctor.py
```

`--days N` changes the window (default 7). `--since` starts it at a point instead: `--since 5h` for the current 5-hour window, `--since 2d`, `--since 1w`, or a date or time such as `--since 2026-10-20` or `--since "2026-10-20 08:00"` (read as your local time). It answers "what used my limit since it last reset". `--json` prints the same findings for scripts, `--md report.md` writes a shareable report with session ids removed. `--no-codex` and `--no-claude` skip one side, and `--claude-dir` / `--codex-dir` point at non-default homes (it also reads `CLAUDE_CONFIG_DIR` and `CODEX_HOME`).

### As a skill

Copy `skills/limit-doctor/` and `limit_doctor.py` into `~/.claude/skills/limit-doctor/` (or `~/.codex/skills/`), then ask your agent why your limit is draining. It runs the script, explains the top three findings and shows the fix commands. It won't remove anything unless you say so.

## How it counts

Plans don't publish their limits, so the percentages are shares of your own usage. Tokens are weighted by API price ratios: an uncached input token is 1, a cache read 0.1, a 5-minute cache write 1.25, a 1-hour cache write 2, output 5. Codex gets its own section and is never added to the Claude total.

What each finding measures:

- Cache rebuilds: a turn that rewrote over 20k tokens of cache after the cache had expired (5 minutes or an hour, depending on the tier). Only the premium over a warm read counts.
- Heavy session start: the first turn's cache write, which is the system prompt, tools, CLAUDE.md and MCP schemas. Flagged when the median is over 30k.
- Unused MCP servers: configured in `~/.claude.json`, `settings.json`, a project `.mcp.json` or `~/.codex/config.toml`, with no calls in the window.
- Context past 100k: the part of each turn's context above 100k, re-read every turn.
- Images: an estimate of 1,600 tokens per image, re-read on every later turn until the session compacts.
- Subagents: everything run in subagent logs, split by model.
- Codex: weighted tokens per day, the cached share of input, and the latest reading of every rate-limit window Codex logs (the 5-hour one and the weekly one), with reset times. Older Codex versions logged these in other shapes; those are read too.

## What it reads

`~/.claude/projects/` (session logs and subagent logs), `~/.codex/sessions/`, and the MCP config files listed above. It only reads. It prints counts, model names, paths and times, never prompt or response text, and it makes no network requests.

## Development

`python -m unittest discover -s tests` runs the tests against the sample logs in `tests/fixtures/`. CI runs them on Python 3.11 to 3.14.

## License

MIT
