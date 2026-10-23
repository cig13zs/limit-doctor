"""Unit tests for limit-doctor."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from limit_doctor import (
    Turn,
    analyze_usage,
    count_image_blocks,
    extract_mcp_servers_from_content,
    find_configured_mcp_servers,
    format_markdown,
    format_text,
    model_expense_tier,
    main,
    normalize_rate_limits,
    parse_since,
    parse_claude_logs,
    parse_codex_logs,
    parse_iso_ts,
    shorten_cwd,
    window_label,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestLimitDoctor(unittest.TestCase):
    def setUp(self):
        self.claude_dir = FIXTURES_DIR / "claude"
        self.codex_dir = FIXTURES_DIR / "codex"
        self.claude_json = FIXTURES_DIR / "claude.json"
        # Window that includes all fixture timestamps (2026-10-19)
        self.cutoff = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_parse_iso_ts(self):
        ts = parse_iso_ts("2026-10-19T10:00:00Z")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertIsNone(parse_iso_ts(None))
        self.assertIsNone(parse_iso_ts("invalid-date"))

    def test_shorten_cwd(self):
        self.assertEqual(shorten_cwd("C:/Users/alice/Desktop/repo"), "Desktop/repo")
        self.assertEqual(shorten_cwd("/workspace/demo/proj1"), "demo/proj1")
        self.assertEqual(shorten_cwd("single"), "single")
        self.assertEqual(shorten_cwd(None), "unknown")

    def test_count_image_blocks(self):
        data = {
            "type": "user",
            "content": [
                {"type": "text", "text": "sample"},
                {"type": "image", "source": {}},
                {"type": "tool_result", "content": [{"type": "image"}]},
            ],
        }
        self.assertEqual(count_image_blocks(data), 2)
        self.assertEqual(count_image_blocks({"type": "text"}), 0)

    def test_extract_mcp_servers(self):
        content = [
            {"type": "tool_use", "name": "mcp__github__create_issue"},
            {"type": "tool_use", "name": "normal_tool"},
            {"type": "text", "text": "hello"},
        ]
        self.assertEqual(extract_mcp_servers_from_content(content), ["github"])

    def test_model_expense_tier(self):
        self.assertEqual(model_expense_tier("claude-opus-5-5"), 3)
        self.assertEqual(model_expense_tier("claude-3-5-sonnet-20241022"), 2)
        self.assertEqual(model_expense_tier("claude-3-5-haiku-20241022"), 1)
        self.assertEqual(model_expense_tier("other-model"), 0)

    def test_claude_log_parsing_dedupe_and_malformed(self):
        """Test dedupe by message.id and skipping of malformed lines."""
        turns, s_imgs, used_m, cwds = parse_claude_logs(self.claude_dir, self.cutoff)
        # In session_test.jsonl: msg_001 is repeated twice, but should be deduped to 1 turn.
        # Plus msg_002, msg_003, msg_004, and subagent sub_msg_001 = 5 turns total.
        self.assertEqual(len(turns), 5)
        # Check that msg_001 is only recorded once
        turn_ids = [t.session_id for t in turns]
        self.assertTrue(all(sid == "sess-test-1" for sid in turn_ids))

        # Check used MCP servers extracted from tool_use
        self.assertIn("used_server", used_m)

        # Check images found in user messages
        self.assertEqual(len(s_imgs.get("sess-test-1", [])), 1)

    def test_idle_rebuild_detection_both_tiers(self):
        """Test idle rebuild detection for 5m tier and 1h tier."""
        turns, s_imgs, used_m, cwds = parse_claude_logs(self.claude_dir, self.cutoff)
        rep = analyze_usage(
            turns=turns,
            session_images=s_imgs,
            used_mcps=used_m,
            configured_mcp=[],
            codex_daily={},
            codex_rl={},
            window_days=7,
        )
        claude_rep = rep["claude"]
        findings = {f["id"]: f for f in claude_rep["cost_findings"]}
        idle = findings.get("idle_rebuilds")
        self.assertIsNotNone(idle)
        # Exactly 2 rebuilds:
        # 1. msg_002: 5m tier, gap = 6 min (360s > 300s), cache write = 25,000 > 20k -> rebuild!
        # (msg_003: 1h tier, gap = 2 min < 3600s -> not rebuild)
        # 2. msg_004: 1h tier, gap = 67 min (4020s > 3600s), cache write = 25,000 > 20k -> rebuild!
        self.assertEqual(idle["count"], 2)
        # only the premium over a warm read: 25k*(1.25-0.1) + 25k*(2.0-0.1)
        self.assertAlmostEqual(idle["weighted_cost"], 76250.0)

    def test_unused_mcp_detection(self):
        """Test unused MCP servers identification across configs."""
        turns, s_imgs, used_m, cwds = parse_claude_logs(self.claude_dir, self.cutoff)
        cfg_m = find_configured_mcp_servers(self.claude_dir, self.claude_json, self.codex_dir, cwds)
        rep = analyze_usage(
            turns=turns,
            session_images=s_imgs,
            used_mcps=used_m,
            configured_mcp=cfg_m,
            codex_daily={},
            codex_rl={},
            window_days=7,
        )
        unused = rep["claude"]["unused_mcps"]
        unused_names = {u["server"] for u in unused}
        # used_server was called in session_test.jsonl, so it must NOT be in unused
        self.assertNotIn("used_server", unused_names)
        # All others should be flagged as unused
        self.assertIn("unused_server_user", unused_names)
        self.assertIn("unused_settings_server", unused_names)
        self.assertNotIn("unused_plugin", unused_names)  # plugins are not MCP servers
        self.assertIn("unused_codex_server", unused_names)
        self.assertIn("unused_proj_server", unused_names)

    def test_codex_null_info_and_malformed(self):
        """Test Codex log parsing handles null info and malformed lines."""
        daily, rl = parse_codex_logs(self.codex_dir, self.cutoff)
        # The null info line and the malformed line should be skipped cleanly
        # Only the valid token_count line should be recorded
        self.assertIn("2026-10-19", daily)
        day_data = daily["2026-10-19"]
        self.assertEqual(day_data["input"], 1000)
        self.assertEqual(day_data["cached"], 400)
        self.assertEqual(day_data["output"], 50)
        # Weighted cost: (1000 - 400)*1.0 + 400*0.1 + 50*5.0 = 600 + 40 + 250 = 890.0
        self.assertAlmostEqual(day_data["weighted"], 890.0)
        # Rate limit
        self.assertEqual(rl["windows"][0]["used_percent"], 25.0)
        self.assertEqual(rl["windows"][0]["resets_at"], 1791949620)

    def test_output_formatters(self):
        """Test text and markdown formatters."""
        turns, s_imgs, used_m, cwds = parse_claude_logs(self.claude_dir, self.cutoff)
        cfg_m = find_configured_mcp_servers(self.claude_dir, self.claude_json, self.codex_dir, cwds)
        daily, rl = parse_codex_logs(self.codex_dir, self.cutoff)
        rep = analyze_usage(turns, s_imgs, used_m, cfg_m, daily, rl, 7)

        text = format_text(rep, is_tty=False)
        self.assertIn("LIMIT DOCTOR v0.1", text)
        self.assertIn("CLAUDE CODE USAGE", text)
        self.assertIn("CODEX", text)
        self.assertNotIn("\u2014", text)  # No em dashes

        md = format_markdown(rep)
        self.assertIn("# Limit Doctor Report", md)
        self.assertIn("| Rank | Finding |", md)
        self.assertIn("session-1", md)  # Masked session id in markdown
        self.assertNotIn("sess-test-1", md)  # No raw session IDs in markdown
        self.assertNotIn("\u2014", md)  # No em dashes

    def test_images_reset_at_compaction(self):
        t0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
        at = lambda m: t0.replace(minute=m)
        turns = [Turn("s1", at(m), "/x", False, "claude-opus-5-5", 0, 50000, 0, 0, 10, "f") for m in (1, 2, 4)]
        events = {"s1": [(at(0), 1), (at(0), 1), (at(3), 0)]}  # two images, then a compaction
        rep = analyze_usage(turns, events, set(), [], {}, {}, 7)
        img = {f["id"]: f for f in rep["claude"]["cost_findings"]}["images"]
        self.assertEqual(img["total_images"], 2)
        # turns at :01 and :02 carry 2 images each, the turn after compaction carries none
        self.assertAlmostEqual(img["weighted_cost"], 2 * 2 * 1600 * 0.1)

    def test_codex_rate_limit_formats(self):
        ts = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        nested = normalize_rate_limits({
            "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1792000000},
            "secondary": {"used_percent": 61.0, "window_minutes": 10080, "resets_in_seconds": 3600},
        }, ts)
        self.assertEqual([w["name"] for w in nested], ["primary", "secondary"])
        self.assertEqual(nested[0]["resets_at"], 1792000000)
        self.assertEqual(nested[1]["resets_at"], ts.timestamp() + 3600)
        flat = normalize_rate_limits({"primary_used_percent": 40.0, "secondary_used_percent": 9.0,
                                      "primary_window_minutes": 300, "secondary_window_minutes": 10080}, ts)
        self.assertEqual([(w["used_percent"], w["window_minutes"], w["resets_at"]) for w in flat],
                         [(40.0, 300, None), (9.0, 10080, None)])
        self.assertEqual(normalize_rate_limits(None, ts), [])
        self.assertEqual(normalize_rate_limits({"primary": None, "secondary": {"used_percent": None}}, ts), [])
        self.assertEqual([window_label(m) for m in (300, 10080, 1440, 45, None)],
                         ["5-hour window", "weekly window", "1-day window", "45-minute window", "unknown window"])

    def test_codex_weekly_window_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "sessions" / "2026" / "10" / "19"
            day.mkdir(parents=True)
            line = {"timestamp": "2026-10-19T12:00:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}},
                "rate_limits": {"primary": {"used_percent": 30.0, "window_minutes": 300, "resets_in_seconds": 600},
                                "secondary": {"used_percent": 75.5, "window_minutes": 10080, "resets_in_seconds": 86400}}}}
            (day / "rollout-a.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
            daily, rl = parse_codex_logs(Path(tmp), self.cutoff)
        rep = analyze_usage([], {}, set(), [], daily, rl, 7)
        self.assertEqual(rep["codex"]["rate_limits"]["used_percent"], 30.0)  # v0.1 shape kept
        self.assertEqual(len(rep["codex"]["rate_limit_windows"]), 2)
        text = format_text(rep, is_tty=False)
        self.assertIn("5-hour window: 30.0% used, resets 2026-10-19 12:10 UTC", text)
        self.assertIn("weekly window: 75.5% used, resets 2026-10-20 12:00 UTC", text)
        self.assertIn("- weekly window: 75.5% used", format_markdown(rep))

    def test_parse_since(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_since("5h", now), now - timedelta(hours=5))
        self.assertEqual(parse_since("2d", now), now - timedelta(days=2))
        self.assertEqual(parse_since("90m", now), now - timedelta(minutes=90))
        self.assertEqual(parse_since("1w", now), now - timedelta(weeks=1))
        self.assertEqual(parse_since("1.5h", now), now - timedelta(minutes=90))
        self.assertEqual(parse_since("2026-10-19T08:00Z", now), datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_since("2026-10-19 08:00+02:00", now), datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))
        local_midnight = datetime(2026, 10, 19).astimezone().astimezone(timezone.utc)
        self.assertEqual(parse_since("2026-10-19", now), local_midnight)
        for bad in ("yesterday", "5x", "h", "", "2026-10-21"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_since(bad, now)

    def test_since_limits_the_window(self):
        args = ["--claude-dir", str(self.claude_dir), "--codex-dir", str(self.codex_dir), "--json"]
        out = io.StringIO()
        with redirect_stdout(out):
            main(args + ["--since", "2026-10-19T10:05:00Z"])
        rep = json.loads(out.getvalue())
        # msg_001 (10:00) and the subagent turn (10:02) fall before the cut
        self.assertEqual(rep["claude"]["total_turns"], 3)
        self.assertEqual(rep["since"], "2026-10-19T10:05:00+00:00")
        self.assertEqual(rep["codex"]["daily"][0]["date"], "2026-10-19")
        out = io.StringIO()
        with redirect_stdout(out):
            main(args[:-1] + ["--since", "2026-10-19T10:05:00Z"])
        self.assertIn("Window: since 2026-10-19 10:05 UTC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
