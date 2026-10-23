---
name: limit-doctor
description: Diagnose token usage drains in Claude Code and Codex and provide actionable fixes. Use when the user asks why their usage or rate limit is running out, what used their tokens, or how to make a limit last longer.
---

# limit-doctor

Diagnose why your usage limit is draining and get the exact fix for each cause.

## Instructions

1. Run the diagnostic tool in machine-readable format:
   ```bash
   python <folder of this SKILL.md>/limit_doctor.py --json
   ```
   The script sits next to this SKILL.md. Use `python3` if `python` is not found.
   If the user asks about a specific stretch ("since my limit reset", "today", "this 5-hour window"),
   add `--since` with a relative value (`5h`, `1d`, `1w`) or a local date/time (`2026-10-20 08:00`).

2. Parse the output JSON:
   - Identify the top 3 findings ranked by weighted token cost.
   - Check for any unused MCP servers or model mix warnings.

3. Explain the top 3 findings in plain language to the user:
   - State what drained tokens (such as idle cache expirations, oversized context windows, or image attachments).
   - Give the token impact and percentage share of total usage.

4. Present the printed fix commands:
   - Clearly display the exact command or setting change for each finding.
   - Do NOT execute any command that modifies configuration or removes servers unless the user explicitly reviews and confirms the action.
