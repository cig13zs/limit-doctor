# Changelog

## Unreleased

### Added

- The daily breakdown is now a bar chart in the text report, one row per day including empty days, with the busiest day against the average. Codex gets the same chart. Days follow your local time zone instead of UTC, and `--md` reports gain a daily table.
- `--since` starts the window at a point instead of `--days`: relative (`5h`, `2d`, `90m`, `1w`) or a date or time in your local zone (`2026-10-20`, `"2026-10-20 08:00"`). The report header and `--json` (`since`) show the start. The skill uses it when you ask about a specific stretch, such as since your limit last reset.

### Fixed

- Codex: only the first rate-limit window was read, so the weekly limit never showed. The report now lists every window in the latest snapshot (5-hour and weekly) with its reset time. It also reads `resets_in_seconds` and the early flat `primary_used_percent` format. In `--json`, `codex.rate_limit_windows` holds the full list; `codex.rate_limits` keeps its v0.1 shape.

## v0.1 (2026-10-20)

First release: ranks Claude Code usage by weighted token cost (idle cache rebuilds, heavy session starts, context past 100k, images, subagents), lists unused MCP servers with the command to remove each, and adds a separate Codex section. Text, `--json` and `--md` output.
