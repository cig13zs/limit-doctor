---
name: limit-doctor
description: Diagnose token usage drains in Claude Code and Codex and provide actionable fixes.
---

# limit-doctor

Diagnose why your usage limit is draining and get the exact fix for each cause.

## Instructions

1. Run the diagnostic tool in machine-readable format:
   ```bash
   python limit_doctor.py --json
   ```

2. Parse the output JSON:
   - Identify the top 3 findings ranked by weighted token cost.
   - Check for any unused MCP servers or model mix warnings.

3. Explain the top 3 findings in plain language to the user:
   - State what drained tokens (such as idle cache expirations, oversized context windows, or image attachments).
   - Give the token impact and percentage share of total usage.

4. Present the printed fix commands:
   - Clearly display the exact command or setting change for each finding.
   - Do NOT execute any command that modifies configuration or removes servers unless the user explicitly reviews and confirms the action.
