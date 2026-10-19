#!/usr/bin/env python3
"""limit-doctor: diagnose token usage drains and report actionable fixes.

Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

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
