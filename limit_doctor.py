#!/usr/bin/env python3
"""limit-doctor: diagnose token usage drains and report actionable fixes.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import tomllib

# Cost model relative to uncached input token:
WEIGHT_INPUT = 1.0
WEIGHT_CACHE_WRITE_5M = 1.25
WEIGHT_CACHE_WRITE_1H = 2.0
WEIGHT_CACHE_READ = 0.1
WEIGHT_OUTPUT = 5.0


def parse_iso_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        s = ts_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def shorten_cwd(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    p = Path(cwd)
    parts = [part for part in p.parts if part not in (p.anchor, "/", "\\")]
    if not parts:
        return p.name or "root"
    return parts[0] if len(parts) == 1 else f"{parts[-2]}/{parts[-1]}"


def count_image_blocks(obj: object) -> int:
    if isinstance(obj, dict):
        cnt = 1 if obj.get("type") == "image" else 0
        return cnt + sum(count_image_blocks(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_image_blocks(item) for item in obj)
    return 0


def extract_mcp_servers_from_content(content: object) -> list[str]:
    srvs = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = b.get("name", "")
                if name.startswith("mcp__"):
                    pts = name.split("__")
                    if len(pts) >= 3:
                        srvs.append(pts[1])
    return srvs


def model_expense_tier(name: str) -> int:
    m = name.lower()
    return 3 if "opus" in m else (2 if "sonnet" in m else (1 if "haiku" in m else 0))


class Turn:
    __slots__ = (
        "session_id", "timestamp", "cwd", "is_sidechain", "model",
        "input_tokens", "cache_read", "cache_write_5m", "cache_write_1h",
        "output_tokens", "file_path", "weighted_cost"
    )

    def __init__(
        self, session_id: str, timestamp: datetime, cwd: str, is_sidechain: bool,
        model: str, input_tokens: int, cache_read: int, cache_write_5m: int,
        cache_write_1h: int, output_tokens: int, file_path: str
    ):
        self.session_id = session_id
        self.timestamp = timestamp
        self.cwd = cwd
        self.is_sidechain = is_sidechain
        self.model = model
        self.input_tokens = input_tokens
        self.cache_read = cache_read
        self.cache_write_5m = cache_write_5m
        self.cache_write_1h = cache_write_1h
        self.output_tokens = output_tokens
        self.file_path = file_path
        write_cost = cache_write_1h * WEIGHT_CACHE_WRITE_1H + cache_write_5m * WEIGHT_CACHE_WRITE_5M
        self.weighted_cost = (
            input_tokens * WEIGHT_INPUT + write_cost +
            cache_read * WEIGHT_CACHE_READ + output_tokens * WEIGHT_OUTPUT
        )

    @property
    def total_cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.total_cache_write


def find_configured_mcp_servers(
    claude_dir: Path, claude_json_path: Path, codex_dir: Path, cwds: set[str]
) -> list[dict[str, str]]:
    cfgs: list[dict[str, str]] = []
    if claude_json_path.is_file():
        try:
            cj = json.loads(claude_json_path.read_text(encoding="utf-8"))
            cfgs.extend({"server": s, "scope": "user", "command": f"claude mcp remove {s} -s user"} for s in cj.get("mcpServers", {}))
            for p, pdata in cj.get("projects", {}).items():
                if isinstance(pdata, dict):
                    cfgs.extend({"server": s, "scope": f"project ({shorten_cwd(p)})", "command": f"claude mcp remove {s} -s project"} for s in pdata.get("mcpServers", {}))
        except Exception:
            pass

    set_p = claude_dir / "settings.json"
    if set_p.is_file():
        try:
            sj = json.loads(set_p.read_text(encoding="utf-8"))
            cfgs.extend({"server": s, "scope": "settings", "command": f"remove mcpServers.{s} in ~/.claude/settings.json"} for s in sj.get("mcpServers", {}))
            cfgs.extend({"server": s, "scope": "plugin", "command": f"set enabledPlugins.{s} to false in ~/.claude/settings.json"} for s, en in sj.get("enabledPlugins", {}).items() if en)
        except Exception:
            pass

    for cwd in cwds:
        pj_file = Path(cwd) / ".mcp.json" if cwd else None
        if pj_file and pj_file.is_file():
            try:
                pj = json.loads(pj_file.read_text(encoding="utf-8"))
                cfgs.extend({"server": s, "scope": f"project ({shorten_cwd(cwd)})", "command": f"claude mcp remove {s} -s project"} for s in pj.get("mcpServers", {}))
            except Exception:
                pass

    c_toml = codex_dir / "config.toml"
    if c_toml.is_file():
        try:
            with open(c_toml, "rb") as f:
                cfgs.extend({"server": s, "scope": "codex", "command": f"remove [mcp_servers.{s}] in ~/.codex/config.toml"} for s in tomllib.load(f).get("mcp_servers", {}))
        except Exception:
            pass
    return cfgs



def parse_claude_logs(
    claude_dir: Path, cutoff: datetime
) -> tuple[list[Turn], dict[str, int], set[str], set[str]]:
    turns: list[Turn] = []
    session_images: dict[str, int] = {}
    used_mcps: set[str] = set()
    cwds: set[str] = set()
    projects_dir = claude_dir / "projects"
    if not projects_dir.is_dir():
        return turns, session_images, used_mcps, cwds

    cutoff_epoch = cutoff.timestamp() - 86400
    seen_dedupe: set[str] = set()
    files = list(projects_dir.glob("*/*.jsonl")) + list(projects_dir.glob("*/subagents/*.jsonl"))

    for fp in files:
        try:
            if fp.stat().st_mtime < cutoff_epoch:
                continue
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    sid = d.get("sessionId") or fp.stem
                    cwd = d.get("cwd") or ""
                    if cwd:
                        cwds.add(cwd)

                    imgs = count_image_blocks(d)
                    if imgs > 0:
                        session_images[sid] = session_images.get(sid, 0) + imgs

                    content = []
                    if isinstance(d.get("content"), list):
                        content.extend(d["content"])
                    msg = d.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                        content.extend(msg["content"])
                    for srv in extract_mcp_servers_from_content(content):
                        used_mcps.add(srv.lower())

                    if d.get("type") == "assistant" and isinstance(msg, dict):
                        mid = msg.get("id") or d.get("requestId")
                        if mid:
                            if mid in seen_dedupe:
                                continue
                            seen_dedupe.add(mid)

                        ts = parse_iso_ts(d.get("timestamp"))
                        if not ts or ts < cutoff:
                            continue

                        u = msg.get("usage") or {}
                        in_tok, out_tok = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
                        rd_tok, cw_tot = u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0
                        cw_dict = u.get("cache_creation")
                        cw_1h = cw_dict.get("ephemeral_1h_input_tokens", 0) or 0 if isinstance(cw_dict, dict) else 0
                        cw_5m = cw_dict.get("ephemeral_5m_input_tokens", 0) or 0 if isinstance(cw_dict, dict) else cw_tot
                        if cw_1h == 0 and cw_5m == 0 and cw_tot > 0:
                            cw_5m = cw_tot

                        is_side = bool(d.get("isSidechain") or "subagents" in fp.parts)
                        model = msg.get("model") or d.get("requestedModel") or "unknown"
                        turns.append(Turn(sid, ts, cwd, is_side, model, in_tok, rd_tok, cw_5m, cw_1h, out_tok, str(fp)))
        except Exception:
            continue
    return turns, session_images, used_mcps, cwds


def parse_codex_logs(
    codex_dir: Path, cutoff: datetime
) -> tuple[dict[str, dict[str, float]], dict[str, object]]:
    daily: dict[str, dict[str, float]] = {}
    latest_rl: dict[str, object] = {}
    latest_ts = 0.0
    s_dir = codex_dir / "sessions"
    if not s_dir.is_dir():
        return daily, latest_rl

    cutoff_epoch = cutoff.timestamp() - 86400
    for fp in s_dir.glob("**/*.jsonl"):
        try:
            if fp.stat().st_mtime < cutoff_epoch:
                continue
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = parse_iso_ts(d.get("timestamp"))
                    if not ts or ts < cutoff:
                        continue
                    payload = d.get("payload")
                    if isinstance(payload, dict) and payload.get("type") == "token_count":
                        info = payload.get("info")
                        if not isinstance(info, dict):
                            continue
                        last_u = info.get("last_token_usage")
                        if isinstance(last_u, dict):
                            in_tok = last_u.get("input_tokens", 0) or 0
                            c_tok = last_u.get("cached_input_tokens", 0) or 0
                            o_tok = last_u.get("output_tokens", 0) or 0
                            w = max(0, in_tok - c_tok) * 1.0 + c_tok * 0.1 + o_tok * 5.0
                            d_str = ts.strftime("%Y-%m-%d")
                            de = daily.setdefault(d_str, {"weighted": 0.0, "input": 0.0, "cached": 0.0, "output": 0.0})
                            de["weighted"] += w
                            de["input"] += in_tok
                            de["cached"] += c_tok
                            de["output"] += o_tok

                        rl = payload.get("rate_limits")
                        if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                            if ts.timestamp() > latest_ts:
                                latest_ts = ts.timestamp()
                                latest_rl = rl["primary"]
        except Exception:
            continue
    return daily, latest_rl


def analyze_usage(
    turns: list[Turn], session_images: dict[str, int], used_mcps: set[str],
    configured_mcp: list[dict[str, str]], codex_daily: dict[str, dict[str, float]],
    codex_rl: dict[str, object], window_days: int
) -> dict[str, object]:
    tot_cost = sum(t.weighted_cost for t in turns)
    sessions: dict[str, list[Turn]] = {}
    for t in turns:
        sessions.setdefault(t.session_id, []).append(t)
    for s_turns in sessions.values():
        s_turns.sort(key=lambda x: x.timestamp)

    def share(c: float) -> float:
        return (c / tot_cost * 100.0) if tot_cost > 0 else 0.0

    # 1. Idle rebuilds
    idle_cnt = 0
    idle_cost = 0.0
    idle_by_s: dict[str, dict[str, float]] = {}
    for sid, s_turns in sessions.items():
        for i in range(1, len(s_turns)):
            prev, curr = s_turns[i - 1], s_turns[i]
            if curr.total_cache_write > 20000:
                dt = (curr.timestamp - prev.timestamp).total_seconds()
                is_1h = curr.cache_write_1h > 0 and curr.cache_write_1h >= curr.cache_write_5m
                if dt > (3600.0 if is_1h else 300.0):
                    idle_cnt += 1
                    idle_cost += curr.weighted_cost
                    e = idle_by_s.setdefault(sid, {"count": 0, "cost": 0.0})
                    e["count"] += 1
                    e["cost"] += curr.weighted_cost

    # 2. Heavy session start
    start_writes = [st[0].total_cache_write for st in sessions.values() if st]
    med_start = statistics.median(start_writes) if start_writes else 0
    worst_start = max(start_writes) if start_writes else 0
    start_cost = sum(st[0].weighted_cost for st in sessions.values() if st)

    # 3. Unused MCPs
    unused_mcps = []
    seen_unused = set()
    for cfg in configured_mcp:
        srv = cfg["server"]
        if srv.lower() not in used_mcps:
            k = (srv.lower(), cfg["scope"])
            if k not in seen_unused:
                seen_unused.add(k)
                unused_mcps.append(cfg)

    # 4. Long contexts
    long_s_cnt = sum(1 for st in sessions.values() if statistics.median([t.context_tokens for t in st]) > 100000)
    turns_past_100k = [t for t in turns if t.context_tokens > 100000]
    cost_past_100k = sum(t.weighted_cost for t in turns_past_100k)

    # 5. Images
    heavy_img_s = {s: cnt for s, cnt in session_images.items() if cnt >= 10}
    s_totals = {s: sum(t.weighted_cost for t in st) for s, st in sessions.items()}
    heavy_img_cost = sum(s_totals[s] for s in heavy_img_s if s in s_totals)

    # 6. Subagents
    sub_turns = [t for t in turns if t.is_sidechain]
    sub_cost = sum(t.weighted_cost for t in sub_turns)
    sub_models: dict[str, float] = {}
    for t in sub_turns:
        sub_models[t.model] = sub_models.get(t.model, 0.0) + t.weighted_cost

    # 7. Model mix
    model_costs: dict[str, float] = {}
    model_short: dict[str, int] = {}
    tot_short = 0
    for t in turns:
        model_costs[t.model] = model_costs.get(t.model, 0.0) + t.weighted_cost
        if t.output_tokens < 200:
            model_short[t.model] = model_short.get(t.model, 0) + 1
            tot_short += 1

    most_exp = sorted(model_costs.keys(), key=lambda m: (model_expense_tier(m), model_costs[m]), reverse=True)[0] if model_costs else None
    flag_short = False
    exp_short_share = 0.0
    if most_exp and tot_short > 0:
        exp_short_share = (model_short.get(most_exp, 0) / tot_short) * 100.0
        flag_short = (model_expense_tier(most_exp) >= 2 and exp_short_share > 50.0)

    cost_findings = sorted([
        {"id": "idle_rebuilds", "name": "Cache rebuilds after idle", "weighted_cost": idle_cost, "share_pct": share(idle_cost),
         "count": idle_cnt, "worst_sessions": sorted(idle_by_s.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:5],
         "fix": "finish or /clear before stepping away; long breaks re-bill the whole context."},
        {"id": "long_contexts", "name": "Long contexts (median context > 100k)", "weighted_cost": cost_past_100k,
         "share_pct": share(cost_past_100k), "session_count": long_s_cnt, "turns_past_100k_count": len(turns_past_100k),
         "fix": "start a new session per task."},
        {"id": "heavy_session_start", "name": "Heavy session start", "weighted_cost": start_cost, "share_pct": share(start_cost),
         "median_write": med_start, "worst_write": worst_start, "flagged": med_start > 30000,
         "fix": "fewer always-on MCP servers and a shorter CLAUDE.md."},
        {"id": "subagents", "name": "Subagents and sidechains", "weighted_cost": sub_cost, "share_pct": share(sub_cost),
         "turn_count": len(sub_turns), "models": sub_models, "fix": "limit autonomous subagent depth and review subagent task scope."},
        {"id": "images", "name": "Sessions with 10+ images", "weighted_cost": heavy_img_cost, "share_pct": share(heavy_img_cost),
         "total_images": sum(session_images.values()), "heavy_session_count": len(heavy_img_s),
         "fix": "crop or downscale screenshots; prefer text reads."},
    ], key=lambda x: x["weighted_cost"], reverse=True)

    # Breakdowns
    pb: dict[str, dict[str, float]] = {}
    db: dict[str, dict[str, float]] = {}
    for t in turns:
        p_name = shorten_cwd(t.cwd)
        pe = pb.setdefault(p_name, {"turns": 0, "cost": 0.0})
        pe["turns"] += 1
        pe["cost"] += t.weighted_cost

        d_str = t.timestamp.strftime("%Y-%m-%d")
        de = db.setdefault(d_str, {"turns": 0, "cost": 0.0})
        de["turns"] += 1
        de["cost"] += t.weighted_cost

    top_s = sorted([
        {"session_id": sid, "date": st[0].timestamp.strftime("%Y-%m-%d"), "project": shorten_cwd(st[0].cwd),
         "turns": len(st), "weighted_cost": sum(t.weighted_cost for t in st), "share_pct": share(sum(t.weighted_cost for t in st))}
        for sid, st in sessions.items()
    ], key=lambda x: x["weighted_cost"], reverse=True)[:5]

    return {
        "window_days": window_days,
        "claude": {
            "total_turns": len(turns), "total_sessions": len(sessions), "total_weighted_tokens": tot_cost,
            "cost_findings": cost_findings, "unused_mcps": unused_mcps,
            "models": {m: {"cost": c, "share_pct": share(c)} for m, c in model_costs.items()},
            "model_short_turns": {"total_short_turns": tot_short, "most_expensive_model": most_exp,
                                  "exp_model_short_share_pct": exp_short_share, "flagged": flag_short},
            "breakdown": {
                "projects": sorted([{"project": k, "turns": int(v["turns"]), "cost": v["cost"], "share_pct": share(v["cost"])} for k, v in pb.items()], key=lambda x: x["cost"], reverse=True),
                "daily": sorted([{"date": k, "turns": int(v["turns"]), "cost": v["cost"], "share_pct": share(v["cost"])} for k, v in db.items()], key=lambda x: x["date"]),
                "top_sessions": top_s,
            },
        },
        "codex": {
            "daily": sorted([{"date": k, "weighted_tokens": v["weighted"], "input_tokens": v["input"], "cached_tokens": v["cached"],
                              "cached_share_pct": (v["cached"] / v["input"] * 100.0) if v["input"] > 0 else 0.0}
                             for k, v in codex_daily.items()], key=lambda x: x["date"]),
            "rate_limits": codex_rl,
        },
    }


def format_text(data: dict[str, object], is_tty: bool) -> str:
    b = "\033[1m" if is_tty else ""
    c = "\033[36m" if is_tty else ""
    r = "\033[0m" if is_tty else ""
    claude = data.get("claude", {})
    tot = claude.get("total_weighted_tokens", 0.0)

    out = [
        f"{b}LIMIT DOCTOR v0.1{r}",
        f"Window: last {data['window_days']} days | Cost model: API ratios (read 0.1x, 5m write 1.25x, 1h write 2.0x, out 5.0x)",
        "Percentages are shares of your own usage, not published plan limits.",
        "-" * 80,
        f"{b}CLAUDE CODE USAGE:{r} {tot:,.0f} weighted tokens across {claude.get('total_turns', 0):,} turns in {claude.get('total_sessions', 0):,} sessions\n",
        f"{b}TOP FINDINGS (ranked by weighted token cost):{r}"
    ]

    for idx, f in enumerate(claude.get("cost_findings", []), 1):
        out.append(f"{c}{idx}. {f['name']}{r} [{f['share_pct']:5.1f}% | {f['weighted_cost']:,.0f} weighted tokens]")
        if f["id"] == "idle_rebuilds":
            out.append(f"   Count: {f['count']} rebuilds")
            worst = f.get("worst_sessions", [])
            if worst:
                out.append("   Worst sessions:")
                for sid, sd in worst[:3]:
                    out.append(f"     - {sid[:12]}...: {int(sd['count'])} rebuilds ({sd['cost']:,.0f} weighted tokens)")
        elif f["id"] == "long_contexts":
            out.append(f"   Sessions with median context > 100k: {f['session_count']} | Turns past 100k: {f['turns_past_100k_count']:,}")
        elif f["id"] == "heavy_session_start":
            st = " [FLAGGED > 30k]" if f.get("flagged") else ""
            out.append(f"   First-turn cache write median: {f['median_write']:,.0f}{st} | worst: {f['worst_write']:,.0f}")
        elif f["id"] == "subagents":
            out.append(f"   Subagent turns: {f['turn_count']}")
            if f.get("models"):
                out.append(f"   Models: {', '.join(f'{m}: {cost:,.0f}' for m, cost in f['models'].items())}")
        elif f["id"] == "images":
            out.append(f"   Total images: {f['total_images']} | Sessions with 10+ images: {f['heavy_session_count']}")
        out.append(f"   Fix: {f['fix']}\n")

    out.append(f"{b}CONFIG & EFFICIENCY FINDINGS:{r}")
    unused = claude.get("unused_mcps", [])
    if unused:
        out.append("Unused MCP servers (configured but 0 calls in window):")
        for u in unused:
            out.append(f"  * {u['server']} ({u['scope']}) -> {u['command']}")
    else:
        out.append("Unused MCP servers: None detected.")

    out.append("Model mix:")
    for m, md in claude.get("models", {}).items():
        out.append(f"  * {m}: {md['cost']:,.0f} weighted tokens ({md['share_pct']:.1f}%)")
    sh = claude.get("model_short_turns", {})
    if sh.get("flagged"):
        out.append(f"  Hint: most short turns (<200 output tokens) ran on {sh['most_expensive_model']} ({sh['exp_model_short_share_pct']:.1f}%),\n  which is the most expensive model.")
    out.append("")

    bd = claude.get("breakdown", {})
    out.append(f"{b}BREAKDOWN:{r}\nProjects (cwd):")
    for p in bd.get("projects", [])[:5]:
        out.append(f"  {p['project']:<35} {p['turns']:>5} turns  {p['cost']:>12,.0f} tokens ({p['share_pct']:5.1f}%)")
    out.append("Daily usage:")
    for d in bd.get("daily", []):
        out.append(f"  {d['date']:<12} {d['turns']:>5} turns  {d['cost']:>12,.0f} tokens ({d['share_pct']:5.1f}%)")
    out.append("Top 5 sessions:")
    for s in bd.get("top_sessions", []):
        sid = s["session_id"][:12] + "..."
        out.append(f"  {sid:<16} {s['date']}  {s['project']:<20} {s['turns']:>4} turns  {s['weighted_cost']:>10,.0f} tokens ({s['share_pct']:5.1f}%)")
    out.append("")

    codex = data.get("codex", {})
    out.append(f"{b}CODEX (separate usage, not summed with Claude):{r}")
    rl = codex.get("rate_limits")
    if isinstance(rl, dict) and rl.get("used_percent") is not None:
        rst = rl.get("resets_at")
        rst_s = datetime.fromtimestamp(rst, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if isinstance(rst, (int, float)) else "unknown"
        out.append(f"Latest rate limits: {rl.get('used_percent')}% used (window: {rl.get('window_minutes')}m, resets at {rst_s})")
    cd = codex.get("daily", [])
    if cd:
        out.append("Daily tokens:")
        for c_entry in cd:
            out.append(f"  {c_entry['date']}: {c_entry['weighted_tokens']:,.0f} weighted tokens (cached input share: {c_entry['cached_share_pct']:.1f}%)")
    else:
        out.append("No Codex token usage recorded in window.")

    return "\n".join(out)


def format_markdown(data: dict[str, object]) -> str:
    claude = data.get("claude", {})
    tot = claude.get("total_weighted_tokens", 0.0)
    lines = [
        "# Limit Doctor Report",
        f"Window: last {data['window_days']} days. Cost model mirrors API price ratios (input 1.0, 5m write 1.25, 1h write 2.0, read 0.1, output 5.0). Percentages represent shares of internal usage.\n",
        f"## Claude Code Usage: {tot:,.0f} weighted tokens\n",
        "### Findings Ranked by Weighted Cost\n",
        "| Rank | Finding | Share | Weighted Tokens | Fix |\n| --- | --- | --- | --- | --- |"
    ]
    for idx, f in enumerate(claude.get("cost_findings", []), 1):
        lines.append(f"| {idx} | {f['name']} | {f['share_pct']:.1f}% | {f['weighted_cost']:,.0f} | {f['fix']} |")
    unused = claude.get("unused_mcps", [])
    u_str = "\n".join(f"- `{u['server']}` ({u['scope']}): `{u['command']}`" for u in unused) if unused else "None detected."
    lines.extend(["\n### Unused MCP Servers", u_str, "\n### Model Mix"])
    for m, md in claude.get("models", {}).items():
        lines.append(f"- `{m}`: {md['cost']:,.0f} weighted tokens ({md['share_pct']:.1f}%)")
    sh = claude.get("model_short_turns", {})
    if sh.get("flagged"):
        lines.append(f"\nHint: most short turns (<200 output tokens) ran on `{sh['most_expensive_model']}` ({sh['exp_model_short_share_pct']:.1f}%), which is the most expensive model.")

    bd = claude.get("breakdown", {})
    lines.append("\n### Projects Breakdown\n| Project | Turns | Weighted Tokens | Share |\n| --- | --- | --- | --- |")
    for p in bd.get("projects", []):
        lines.append(f"| {p['project']} | {p['turns']} | {p['cost']:,.0f} | {p['share_pct']:.1f}% |")

    lines.append("\n### Top Sessions\n| Session | Date | Project | Turns | Weighted Tokens | Share |\n| --- | --- | --- | --- | --- | --- |")
    for idx, s in enumerate(bd.get("top_sessions", []), 1):
        lines.append(f"| session-{idx} | {s['date']} | {s['project']} | {s['turns']} | {s['weighted_cost']:,.0f} | {s['share_pct']:.1f}% |")

    codex = data.get("codex", {})
    lines.append("\n## Codex Usage")
    rl = codex.get("rate_limits")
    if isinstance(rl, dict) and rl.get("used_percent") is not None:
        rst = rl.get("resets_at")
        rst_s = datetime.fromtimestamp(rst, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if isinstance(rst, (int, float)) else "unknown"
        lines.append(f"- Rate limits: {rl.get('used_percent')}% used (window: {rl.get('window_minutes')}m, resets at {rst_s})")
    for c_entry in codex.get("daily", []):
        lines.append(f"- {c_entry['date']}: {c_entry['weighted_tokens']:,.0f} weighted tokens (cached input share: {c_entry['cached_share_pct']:.1f}%)")
    return "\n".join(lines)



def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="limit_doctor", description="Diagnose token usage drains and report actionable fixes.")
    for flag, kw in [
        ("--days", {"type": int, "default": 7, "help": "Window in days"}),
        ("--json", {"action": "store_true", "help": "Machine output"}),
        ("--md", {"type": str, "default": "", "help": "Markdown output file"}),
        ("--claude-dir", {"type": str, "default": "", "help": "Claude config dir"}),
        ("--codex-dir", {"type": str, "default": "", "help": "Codex config dir"}),
        ("--no-claude", {"action": "store_true", "help": "Skip Claude"}),
        ("--no-codex", {"action": "store_true", "help": "Skip Codex"}),
    ]:
        p.add_argument(flag, **kw)
    args = p.parse_args(argv)

    c_dir = Path(args.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    cx_dir = Path(args.codex_dir or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    cj_path = Path.home() / ".claude.json"
    for cand in (c_dir.parent / ".claude.json", c_dir.parent / "claude.json", c_dir / "claude.json"):
        if cand.is_file():
            cj_path = cand
            break
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    turns, s_imgs, used_m, cwds = parse_claude_logs(c_dir, cutoff) if not args.no_claude else ([], {}, set(), set())
    cfg_m = find_configured_mcp_servers(c_dir, cj_path, cx_dir, cwds) if not args.no_claude else []
    cx_daily, cx_rl = parse_codex_logs(cx_dir, cutoff) if not args.no_codex else ({}, {})
    rep = analyze_usage(turns, s_imgs, used_m, cfg_m, cx_daily, cx_rl, args.days)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(format_text(rep, is_tty=sys.stdout.isatty()))
    if args.md:
        try:
            Path(args.md).write_text(format_markdown(rep), encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"Warning: failed to write markdown to {args.md}: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

