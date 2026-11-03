"""Unified local Antigravity storage, transcript parser, and agentapi bridge."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit
import urllib.request
import ssl

from ..i18n import tr as _t
from ..paths import antigravity_home, ensure_data_dir
from ..platforms.processes import spawn_options

DEFAULT_CONTEXT_WINDOW = 1_048_576
ANTIGRAVITY_MODELS = [
    {
        "model": "pro",
        "displayName": "Gemini 2.5 Pro (pro)",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "high"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "low"},
        ],
    },
    {
        "model": "flash",
        "displayName": "Gemini 2.5 Flash (flash)",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "low"},
        ],
    },
    {
        "model": "flash_lite",
        "displayName": "Gemini 2.5 Flash Lite (flash_lite)",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
        ],
    },
]


def summaries_db_path(home: Path | None = None) -> Path:
    root = home or antigravity_home()
    direct = root / "conversation_summaries.db"
    if direct.is_file():
        return direct
    nested = root / "conversations" / "conversation_summaries.db"
    if nested.is_file():
        return nested
    return direct


def brain_root(home: Path | None = None) -> Path:
    return (home or antigravity_home()) / "brain"


def brain_dir(thread_id: str, home: Path | None = None) -> Path:
    return brain_root(home) / str(thread_id)


def transcript_path(thread_id: str, home: Path | None = None) -> Path:
    return brain_dir(thread_id, home) / ".system_generated" / "logs" / "transcript.jsonl"


def transcript_full_path(thread_id: str, home: Path | None = None) -> Path:
    return brain_dir(thread_id, home) / ".system_generated" / "logs" / "transcript_full.jsonl"


def has_antigravity_data(home: Path | None = None) -> bool:
    root = home or antigravity_home()
    return summaries_db_path(root).is_file() or (root / "brain").is_dir()


def parse_iso_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Python 3.10 fromisoformat may reject 9-digit nanoseconds; trim fractional digits to 6.
    raw = re.sub(r"\.(\d{6})\d+([+-]\d{2}:\d{2})$", r".\1\2", raw)
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def parse_workspace_uris(raw: Any) -> str:
    uris: list[str] = []
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                uris = [str(item) for item in parsed if item]
            elif isinstance(parsed, str):
                uris = [parsed]
        except ValueError:
            uris = [raw.strip()]
    elif isinstance(raw, list):
        uris = [str(item) for item in raw if item]
    if not uris:
        return ""
    first = uris[0].strip()
    if first.lower().startswith("file:"):
        split = urlsplit(first)
        path = unquote(split.path)
        if split.netloc and split.netloc.lower() != "localhost":
            path = "//" + split.netloc + path
        if re.match(r"^/[a-zA-Z]:/", path):
            path = path[1:]
        return str(Path(path))
    return first


def clean_user_input_text(raw: Any) -> str:
    text = str(raw or "")
    if not text:
        return ""
    matches = re.findall(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", text)
    if matches:
        cleaned = "\n\n".join(m.strip() for m in matches if m.strip())
        if cleaned:
            return cleaned
    text = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", "", text)
    text = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", text)
    text = re.sub(r"<SYSTEM_MESSAGE>[\s\S]*?</SYSTEM_MESSAGE>", "", text)
    return text.strip()


def _connect_readonly_sqlite(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_conversation_summary(thread_id: str, home: Path | None = None) -> dict[str, Any] | None:
    if not thread_id or "/" in thread_id or "\\" in thread_id or ".." in thread_id:
        return None
    root = home or antigravity_home()
    db_file = summaries_db_path(root)
    if db_file.is_file():
        try:
            with closing(_connect_readonly_sqlite(db_file)) as db:
                row = db.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                    (thread_id,),
                ).fetchone()
                if row is not None:
                    return _normalize_summary_row(dict(row), root)
        except sqlite3.Error:
            pass
    t_path = transcript_path(thread_id, root)
    if t_path.is_file():
        return _synthesize_summary_from_transcript(thread_id, t_path, root)
    return None


def _is_waiting_on_ask_question(thread_id: str, home: Path) -> bool:
    t_path = transcript_path(thread_id, home)
    if not t_path.is_file():
        return False
    try:
        with t_path.open("rb") as handle:
            size = t_path.stat().st_size
            handle.seek(max(0, size - 32768))
            tail = handle.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            step = json.loads(line)
            if not isinstance(step, dict):
                return False
            if step.get("type") != "PLANNER_RESPONSE":
                return False
            for call in step.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("name") == "ask_question":
                    return True
            return False
    except (OSError, ValueError, TypeError):
        return False
    return False


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return value if isinstance(value, list) else []


def _parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _normalize_summary_row(row: Mapping[str, Any], home: Path) -> dict[str, Any]:
    cid = str(row.get("conversation_id") or "")
    status_raw = str(row.get("status") or "")
    killed = bool(row.get("killed"))
    not_fully_idle = bool(row.get("not_fully_idle"))
    active = not killed and (
        status_raw == "CASCADE_RUN_STATUS_RUNNING" or not_fully_idle
    )
    active_flags = ["waitingOnUserInput"] if active and _is_waiting_on_ask_question(cid, home) else []
    cwd = parse_workspace_uris(row.get("workspace_uris"))
    title = str(row.get("title") or "").strip()
    preview = clean_user_input_text(row.get("preview") or "")
    if not title and not preview and cid:
        t_path = transcript_path(cid, home)
        if t_path.is_file():
            synth = _synthesize_summary_from_transcript(cid, t_path, home)
            title = str(synth.get("title") or "").strip()
            preview = str(synth.get("preview") or "").strip()
    updated_at = parse_iso_timestamp(row.get("last_modified_time")) or 0.0
    nesting_depth = int(row.get("nesting_depth") or 0)
    parent_id = str(row.get("parent_conversation_id") or "").strip()
    return {
        "id": cid,
        "name": title or preview[:80] or cid[:8],
        "title": title or preview[:80] or cid[:8],
        "preview": preview or title or "",
        "cwd": cwd,
        "status": {
            "type": "active" if active else "idle",
            "raw": status_raw,
            "activeFlags": active_flags,
        },
        "updatedAt": int(updated_at) if updated_at else 0,
        "stepCount": int(row.get("step_count") or 0),
        "source": str(row.get("source") or "antigravity"),
        "agentName": str(row.get("agent_name") or ""),
        "parentConversationId": parent_id,
        "nestingDepth": nesting_depth,
        "isSubagent": bool(nesting_depth > 0 or parent_id),
        "killed": killed,
        "appDataDir": str(row.get("app_data_dir") or home),
    }


def _synthesize_summary_from_transcript(thread_id: str, t_path: Path, home: Path) -> dict[str, Any]:
    steps = read_transcript_steps(thread_id, home=home, explicit_path=t_path)
    first_user = ""
    last_text = ""
    updated_at = 0.0
    for step in steps:
        ts = parse_iso_timestamp(step.get("created_at"))
        if ts:
            updated_at = max(updated_at, ts)
        if step.get("type") == "USER_INPUT" and step.get("source") == "USER_EXPLICIT":
            cleaned = clean_user_input_text(step.get("content") or "")
            if cleaned and not first_user:
                first_user = cleaned
        elif step.get("type") == "PLANNER_RESPONSE" and step.get("content"):
            last_text = str(step.get("content") or "").strip()
    if not updated_at:
        try:
            updated_at = t_path.stat().st_mtime
        except OSError:
            updated_at = 0.0
    active = False
    active_flags: list[str] = []
    if steps:
        last_step = steps[-1]
        if str(last_step.get("status") or "").upper() in {"RUNNING", "IN_PROGRESS", "PENDING"}:
            active = True
        if active and _is_waiting_on_ask_question(thread_id, home):
            active_flags = ["waitingOnUserInput"]
    title = first_user.splitlines()[0][:80] if first_user else thread_id[:8]
    return {
        "id": thread_id,
        "name": title,
        "title": title,
        "preview": first_user or last_text[:120] or title,
        "cwd": "",
        "status": {"type": "active" if active else "idle", "activeFlags": active_flags},
        "updatedAt": int(updated_at),
        "stepCount": len(steps),
        "source": "antigravity",
        "agentName": "",
        "parentConversationId": "",
        "nestingDepth": 0,
        "isSubagent": False,
        "killed": False,
        "appDataDir": str(home),
    }


def list_antigravity_threads(
    home: Path | None = None,
    *,
    limit: int = 100,
    include_subagents: bool = False,
) -> list[dict[str, Any]]:
    root = home or antigravity_home()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    db_file = summaries_db_path(root)
    if db_file.is_file():
        try:
            with closing(_connect_readonly_sqlite(db_file)) as db:
                if include_subagents:
                    query = (
                        "SELECT * FROM conversation_summaries "
                        "ORDER BY last_modified_time DESC LIMIT ?"
                    )
                    rows = db.execute(query, (max(1, limit),)).fetchall()
                else:
                    query = (
                        "SELECT * FROM conversation_summaries "
                        "WHERE COALESCE(nesting_depth, 0) = 0 "
                        "AND COALESCE(parent_conversation_id, '') = '' "
                        "ORDER BY last_modified_time DESC LIMIT ?"
                    )
                    rows = db.execute(query, (max(1, limit),)).fetchall()
                for row in rows:
                    item = _normalize_summary_row(dict(row), root)
                    cid = item["id"]
                    if cid and cid not in seen:
                        seen.add(cid)
                        results.append(item)
        except sqlite3.Error:
            pass
    b_root = brain_root(root)
    if len(results) < limit and b_root.is_dir():
        try:
            candidates = []
            for entry in b_root.iterdir():
                if not entry.is_dir() or entry.name.startswith(".") or entry.name in seen:
                    continue
                t_file = entry / ".system_generated" / "logs" / "transcript.jsonl"
                if t_file.is_file():
                    try:
                        candidates.append((t_file.stat().st_mtime, entry.name, t_file))
                    except OSError:
                        continue
            candidates.sort(reverse=True)
            for _mtime, cid, t_file in candidates[: max(0, limit - len(results))]:
                seen.add(cid)
                results.append(_synthesize_summary_from_transcript(cid, t_file, root))
        except OSError:
            pass
    results.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
    return results[:limit]


def read_transcript_steps(
    thread_id: str,
    *,
    home: Path | None = None,
    explicit_path: Path | None = None,
) -> list[dict[str, Any]]:
    root = home or antigravity_home()
    path = explicit_path or transcript_path(thread_id, root)
    if not path.is_file():
        return []
    full_path = path.with_name("transcript_full.jsonl")
    steps: list[dict[str, Any]] = []
    needs_full: list[tuple[int, int | None]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_idx, raw_line in enumerate(handle):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                steps.append(record)
                truncated = record.get("truncated_fields")
                if isinstance(truncated, list) and any(
                    f in truncated for f in ("content", "tool_calls")
                ):
                    step_idx = record.get("step_index")
                    needs_full.append(( len(steps) - 1, step_idx if isinstance(step_idx, int) else None ))
    except OSError:
        return steps

    if needs_full and full_path.is_file():
        wanted_step_indices = {s_idx for _, s_idx in needs_full if s_idx is not None}
        wanted_list_indices = {l_idx for l_idx, s_idx in needs_full if s_idx is None}
        full_by_step: dict[int, dict[str, Any]] = {}
        full_by_pos: dict[int, dict[str, Any]] = {}
        try:
            with full_path.open("r", encoding="utf-8", errors="replace") as handle:
                pos = 0
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        pos += 1
                        continue
                    if isinstance(record, dict):
                        s_idx = record.get("step_index")
                        if isinstance(s_idx, int) and s_idx in wanted_step_indices:
                            full_by_step[s_idx] = record
                        if pos in wanted_list_indices:
                            full_by_pos[pos] = record
                    pos += 1
        except OSError:
            pass
        for list_idx, step_idx in needs_full:
            replacement = (
                full_by_step.get(step_idx)
                if step_idx is not None
                else full_by_pos.get(list_idx)
            )
            if isinstance(replacement, dict):
                steps[list_idx] = replacement
    return steps


def _format_tool_diff(tool_name: str, args: Mapping[str, Any]) -> tuple[str, str]:
    file_path = str(args.get("Path") or args.get("TargetFile") or "").strip()
    if not file_path:
        return "", ""
    display_path = file_path.replace("\\", "/")
    if tool_name == "edit_file":
        target = str(args.get("Target") or "")
        replacement = str(args.get("Replacement") or "")
        diff_lines = list(
            difflib.unified_diff(
                target.splitlines(),
                replacement.splitlines(),
                fromfile=f"a/{display_path}",
                tofile=f"b/{display_path}",
                lineterm="",
            )
        )
        if diff_lines:
            return display_path, "\n".join(diff_lines[:200])
    elif tool_name == "write_file":
        content = str(args.get("Content") or "")
        lines = content.splitlines()[:80]
        body = "\n".join(f"+{line}" for line in lines)
        return (
            display_path,
            f"--- /dev/null\n+++ b/{display_path}\n@@ -0,0 +1,{len(lines)} @@\n{body}",
        )
    return "", ""


def build_thread_from_antigravity(
    thread_id: str,
    *,
    home: Path | None = None,
    include_turns: bool = True,
) -> dict[str, Any] | None:
    root = home or antigravity_home()
    summary = read_conversation_summary(thread_id, root)
    if summary is None:
        return None
    if not include_turns:
        return dict(summary)
    steps = read_transcript_steps(thread_id, home=root)
    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    active = summary.get("status", {}).get("type") == "active"

    for step in steps:
        step_idx = step.get("step_index", len(turns))
        step_type = str(step.get("type") or "")
        step_source = str(step.get("source") or "")
        ts = parse_iso_timestamp(step.get("created_at"))

        if step_type == "USER_INPUT" and step_source == "USER_EXPLICIT":
            user_text = clean_user_input_text(step.get("content") or "")
            if not user_text:
                continue
            if current_turn is not None:
                current_turn["status"] = "completed"
                turns.append(current_turn)
            current_turn = {
                "id": f"turn-{step_idx}",
                "status": "completed",
                "startedAt": ts,
                "_lastTs": ts,
                "durationMs": 0,
                "items": [
                    {
                        "id": f"item-{step_idx}",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": user_text}],
                    }
                ],
            }
            continue

        if current_turn is None:
            current_turn = {
                "id": f"turn-{step_idx}",
                "status": "completed",
                "startedAt": ts,
                "_lastTs": ts,
                "durationMs": 0,
                "items": [],
            }
        if ts and current_turn.get("startedAt"):
            current_turn["durationMs"] = int(max(0.0, ts - float(current_turn["startedAt"])) * 1000)
        elif ts and not current_turn.get("startedAt"):
            current_turn["startedAt"] = ts

        if step_type == "GENERIC" and current_turn.get("_pending_ask"):
            pending_id, pending_qs = current_turn.pop("_pending_ask")
            raw_content = str(step.get("content") or "")
            answers_map: dict[int, str] = {}
            for line in raw_content.splitlines():
                m = re.match(r"^\s*A(\d+)\s*:\s*(.+)$", line)
                if m:
                    answers_map[int(m.group(1)) - 1] = m.group(2).strip()
            reply_entries = []
            for q_idx, q in enumerate(pending_qs):
                ans = answers_map.get(q_idx) or answers_map.get(0) or "已回答"
                qid = json.dumps(["request_user_input_async", pending_id, q_idx], separators=(",", ":"))
                reply_entries.append({"questionItemId": qid, "question": q["title"], "answer": ans})
            if reply_entries:
                reply_text = (
                    "<send_user_message_question_reply>\n"
                    + json.dumps(reply_entries, ensure_ascii=False)
                    + "\n</send_user_message_question_reply>"
                )
                current_turn["items"].append(
                    {
                        "id": f"item-ans-{step_idx}",
                        "type": "steeringUserMessage",
                        "content": [{"type": "text", "text": reply_text}],
                    }
                )

        if step_type == "PLANNER_RESPONSE":
            tool_calls = step.get("tool_calls")
            if isinstance(tool_calls, list):
                for call_idx, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        continue
                    t_name = str(call.get("name") or "")
                    raw_t_args = call.get("args") if call.get("args") is not None else call.get("arguments")
                    t_args = _parse_json_dict(raw_t_args)
                    item_id = str(call.get("id") or f"tool-{step_idx}-{call_idx}")
                    if t_name == "run_command":
                        cmd = str(t_args.get("CommandLine") or t_args.get("toolSummary") or "run_command")
                        current_turn["items"].append(
                            {"id": item_id, "type": "commandExecution", "command": cmd}
                        )
                    elif t_name in {"edit_file", "write_file"}:
                        changed_path, diff_text = _format_tool_diff(t_name, t_args)
                        if changed_path:
                            current_turn["items"].append(
                                {
                                    "id": item_id,
                                    "type": "fileChange",
                                    "changes": [{"path": changed_path, "diff": diff_text}],
                                }
                            )
                    elif t_name == "call_mcp_tool":
                        current_turn["items"].append(
                            {
                                "id": item_id,
                                "type": "mcpToolCall",
                                "server": str(t_args.get("ServerName") or "mcp"),
                                "tool": str(t_args.get("ToolName") or "tool"),
                            }
                        )
                    elif t_name == "ask_question":
                        raw_qs = _parse_json_list(t_args.get("questions"))
                        normalized_qs = []
                        for q in raw_qs:
                            if not isinstance(q, dict):
                                continue
                            q_title = str(q.get("question") or q.get("title") or "").strip()
                            if not q_title:
                                continue
                            q_opts = [str(o) for o in (q.get("options") or []) if isinstance(o, (str, int, float))]
                            normalized_qs.append({"title": q_title, "options": q_opts})
                        if normalized_qs:
                            current_turn["items"].append(
                                {
                                    "id": item_id,
                                    "type": "agentMessage",
                                    "phase": "commentary",
                                    "text": normalized_qs[0]["title"],
                                    "questions": normalized_qs,
                                }
                            )
                            current_turn["_pending_ask"] = (item_id, normalized_qs)
                    elif t_name:
                        summary_arg = str(
                            t_args.get("Path")
                            or t_args.get("query")
                            or t_args.get("Role")
                            or t_args.get("Action")
                            or ""
                        )
                        label = f"{t_name} {summary_arg}".strip()
                        current_turn["items"].append(
                            {"id": item_id, "type": "commandExecution", "command": label}
                        )
            content = str(step.get("content") or "").strip()
            if content:
                # Mark intermediate messages before tool calls as commentary if tools were also called,
                # and later upgrade the final agentMessage in each turn to final_answer.
                has_tools = bool(step.get("tool_calls"))
                current_turn["items"].append(
                    {
                        "id": f"agent-{step_idx}",
                        "type": "agentMessage",
                        "phase": "commentary" if has_tools else "final_answer",
                        "text": content,
                    }
                )

    if current_turn is not None:
        current_turn["status"] = "inProgress" if active else "completed"
        turns.append(current_turn)

    # Ensure the last agentMessage in each turn has phase='final_answer' so extract_messages finds it.
    for turn in turns:
        turn.pop("_lastTs", None)
        turn.pop("_pending_ask", None)
        agent_items = [
            item
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and not item.get("questions")
        ]
        if agent_items:
            agent_items[-1]["phase"] = "final_answer"

    thread = dict(summary)
    thread["turns"] = turns
    return thread


def compute_antigravity_token_usage(
    thread_id: str,
    home: Path | None = None,
) -> dict[str, Any] | None:
    steps = read_transcript_steps(thread_id, home=home)
    if not steps:
        return None
    total_in = 0
    total_cache = 0
    total_out = 0
    last_in = 0
    last_cache = 0
    last_out = 0
    has_tokens = False
    for step in steps:
        in_tok = step.get("input_tokens")
        cache_tok = step.get("cache_read_tokens")
        out_tok = step.get("output_tokens")
        if any(isinstance(v, int) and v > 0 for v in (in_tok, cache_tok, out_tok)):
            has_tokens = True
            i_val = int(in_tok or 0)
            c_val = int(cache_tok or 0)
            o_val = int(out_tok or 0)
            total_in += i_val
            total_cache += c_val
            total_out += o_val
            last_in = i_val
            last_cache = c_val
            last_out = o_val
    if not has_tokens:
        return None
    total_all = total_in + total_cache + total_out
    last_all = last_in + last_cache + last_out
    return {
        "total": {
            "totalTokens": total_all,
            "inputTokens": total_in,
            "cachedInputTokens": total_cache,
            "outputTokens": total_out,
            "reasoningOutputTokens": 0,
        },
        "last": {
            "totalTokens": last_all,
            "inputTokens": last_in,
            "cachedInputTokens": last_cache,
            "outputTokens": last_out,
        },
        "modelContextWindow": DEFAULT_CONTEXT_WINDOW,
    }


def _discover_live_language_server() -> list[tuple[str, str]]:
    """Auto-discover running Antigravity language_server (address, csrf_token) pairs."""
    candidates: list[tuple[str, str]] = []
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            name = str(proc.info.get("name") or "").lower()
            if "language_server" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not isinstance(cmdline, list):
                continue
            csrf = ""
            for idx, arg in enumerate(cmdline):
                s = str(arg or "")
                if s == "--csrf_token" and idx + 1 < len(cmdline):
                    csrf = str(cmdline[idx + 1] or "").strip()
                    break
                if s.startswith("--csrf_token="):
                    csrf = s.partition("=")[2].strip()
                    break
            if not csrf:
                continue
            try:
                conns = proc.net_connections(kind="inet")
            except Exception:
                continue
            ports = sorted(
                {
                    int(c.laddr.port)
                    for c in conns
                    if getattr(c, "status", "") == "LISTEN"
                    and getattr(c, "laddr", None)
                    and getattr(c.laddr, "port", 0)
                },
                reverse=True,
            )
            for port in ports:
                pair = (f"localhost:{port}", csrf)
                if pair not in candidates:
                    candidates.append(pair)
    except Exception:
        pass
    return candidates


def _call_ls_rpc(
    address: str,
    csrf_token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    addr = address.strip()
    if not addr:
        return None
    if "://" in addr:
        addr = addr.split("://", 1)[1]
    body_bytes = json.dumps(payload or {}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if csrf_token:
        headers["x-codeium-csrf-token"] = csrf_token
    for scheme in ("http", "https"):
        url = f"{scheme}://{addr}/exa.language_server_pb.LanguageServerService/{method}"
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        ctx = ssl._create_unverified_context() if scheme == "https" else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            continue
    return None


def _parse_iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        return None


def _call_live_ls_rpc(
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    data: Path | None = None,
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    runtime_info = load_runtime_env(data)
    candidates: list[tuple[str, str]] = []
    saved_addr = str(runtime_info.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    saved_csrf = str(runtime_info.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    if saved_addr:
        candidates.append((saved_addr, saved_csrf))
    if data is None:
        for pair in _discover_live_language_server():
            if pair not in candidates:
                candidates.append(pair)
    for addr, csrf in candidates:
        res = _call_ls_rpc(addr, csrf, method, payload or {}, timeout=timeout)
        if isinstance(res, dict):
            if (addr != saved_addr or csrf != saved_csrf) and data is None:
                try:
                    save_runtime_env(
                        {"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": csrf},
                        data=data,
                    )
                except Exception:
                    pass
            return res
    return None


def fetch_antigravity_quota_groups(
    data: Path | None = None,
    timeout: float = 4.0,
) -> list[dict[str, Any]]:
    """Query Antigravity Language Server RetrieveUserQuotaSummary for Gemini and Claude/GPT quotas."""
    res = _call_live_ls_rpc("RetrieveUserQuotaSummary", {}, data=data, timeout=timeout)
    raw_response = res.get("response") if isinstance(res, dict) else None
    if not isinstance(raw_response, dict):
        return []
    groups = raw_response.get("groups")
    if not isinstance(groups, list):
        return []
    normalized_groups: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        display_name = str(group.get("displayName") or "").strip()
        raw_buckets = group.get("buckets")
        if not isinstance(raw_buckets, list):
            continue
        buckets: list[dict[str, Any]] = []
        for b in raw_buckets:
            if not isinstance(b, dict):
                continue
            window = str(b.get("window") or "").strip()
            bucket_id = str(b.get("bucketId") or "").strip()
            try:
                rem_frac = float(b.get("remainingFraction", 0.0) or 0.0)
            except (TypeError, ValueError):
                rem_frac = 0.0
            rem_frac = max(0.0, min(1.0, rem_frac))
            used_percent = round((1.0 - rem_frac) * 100.0, 1)
            resets_at = _parse_iso_epoch(b.get("resetTime"))
            buckets.append(
                {
                    "bucketId": bucket_id,
                    "window": window,
                    "remainingFraction": rem_frac,
                    "usedPercent": used_percent,
                    "resetsAt": resets_at,
                    "resetTime": str(b.get("resetTime") or ""),
                }
            )
        if buckets:
            normalized_groups.append(
                {
                    "displayName": display_name,
                    "buckets": buckets,
                }
            )
    return normalized_groups


def fetch_antigravity_models(
    data: Path | None = None,
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """Query Antigravity Language Server GetCascadeModelConfigData for available models."""
    res = _call_live_ls_rpc("GetCascadeModelConfigData", {}, data=data, timeout=timeout)
    configs = res.get("clientModelConfigs") if isinstance(res, dict) else None
    if not isinstance(configs, list) or not configs:
        return list(ANTIGRAVITY_MODELS)
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        model_id = str(cfg.get("modelId") or "").strip()
        label = str(cfg.get("label") or model_id).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "model": model_id,
                "displayName": f"{label} ({model_id})" if label and label != model_id else model_id,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "low"},
                ],
            }
        )
    return models if models else list(ANTIGRAVITY_MODELS)


def fetch_antigravity_account(
    data: Path | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Query Antigravity Language Server GetUserStatus for real account identity and plan."""
    res = _call_live_ls_rpc("GetUserStatus", {}, data=data, timeout=timeout)
    user_status = res.get("userStatus") if isinstance(res, dict) else None
    if isinstance(user_status, dict):
        email = str(user_status.get("email") or "").strip()
        plan_status = user_status.get("planStatus") if isinstance(user_status.get("planStatus"), dict) else {}
        plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}
        plan_name = str(plan_info.get("planName") or "antigravity").strip()
        if email:
            return {"type": "chatgpt", "email": email, "planType": plan_name}
    return {"type": "chatgpt", "email": "local@antigravity", "planType": "antigravity"}


def cancel_antigravity_cascade(
    thread_id: str,
    *,
    data: Path | None = None,
    timeout: float = 4.0,
) -> bool:
    """Immediately cancel a running Antigravity cascade via Language Server RPC + hook queue fallback."""
    if not thread_id:
        return False
    queue_pending_control(thread_id, stop=True, data=data)
    res_cancel = _call_live_ls_rpc(
        "CancelCascadeInvocation",
        {"cascadeId": thread_id},
        data=data,
        timeout=timeout,
    )
    res_stop = _call_live_ls_rpc(
        "ForceStopCascadeTree",
        {"conversationId": thread_id},
        data=data,
        timeout=timeout,
    )
    return res_cancel is not None or res_stop is not None


def aggregate_antigravity_quota_stats(
    home: Path | None = None,
    data: Path | None = None,
    current_model: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    root = home or antigravity_home()
    runtime_info = load_runtime_env(data)
    model_name = (
        current_model
        or runtime_info.get("model_name")
        or "pro (Gemini 2.5 Pro)"
    )
    threads = list_antigravity_threads(root, limit=limit, include_subagents=False)
    active_count = sum(
        1 for t in threads if (t.get("status") or {}).get("type") == "active"
    )
    sum_in = 0
    sum_cache = 0
    sum_out = 0
    recent_usage: list[dict[str, Any]] = []
    for thread in threads:
        tid = str(thread.get("id") or "")
        usage = compute_antigravity_token_usage(tid, root)
        if not usage:
            continue
        total = usage["total"]
        sum_in += int(total.get("inputTokens") or 0)
        sum_cache += int(total.get("cachedInputTokens") or 0)
        sum_out += int(total.get("outputTokens") or 0)
        if len(recent_usage) < 5:
            recent_usage.append(
                {
                    "id": tid,
                    "title": str(thread.get("name") or tid[:8]),
                    "active": (thread.get("status") or {}).get("type") == "active",
                    "totalTokens": int(total.get("totalTokens") or 0),
                    "inputTokens": int(total.get("inputTokens") or 0),
                    "cachedInputTokens": int(total.get("cachedInputTokens") or 0),
                    "outputTokens": int(total.get("outputTokens") or 0),
                }
            )
    quota_groups = fetch_antigravity_quota_groups(data=data)
    runtime_info = load_runtime_env(data)
    return {
        "antigravityUsage": {
            "currentModel": model_name,
            "totalConversations": len(threads),
            "activeConversations": active_count,
            "totalTokens": sum_in + sum_cache + sum_out,
            "inputTokens": sum_in,
            "cachedInputTokens": sum_cache,
            "outputTokens": sum_out,
            "recentTasks": recent_usage,
            "lsConnected": bool(runtime_info.get("ANTIGRAVITY_LS_ADDRESS")) or bool(quota_groups),
            "quotaGroups": quota_groups,
        }
    }


def _runtime_env_path(data: Path | None = None) -> Path:
    return (data or ensure_data_dir()) / "antigravity_runtime.json"


def save_runtime_env(
    env: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
    data: Path | None = None,
) -> dict[str, Any]:
    source_env = env if env is not None else os.environ
    path = _runtime_env_path(data)
    existing = load_runtime_env(data)
    updated = dict(existing)
    for key in (
        "ANTIGRAVITY_LS_ADDRESS",
        "ANTIGRAVITY_CSRF_TOKEN",
        "ANTIGRAVITY_AGENTAPI_EXE",
        "ANTIGRAVITY_APP_DATA_DIR",
        "ANTIGRAVITY_LS_VERSION",
        "ANTIGRAVITY_PROJECT_ID",
        "AGY_SERVER_ADDRESS",
        "AGY_CSRF_TOKEN",
    ):
        val = str(source_env.get(key) or "").strip()
        if val:
            updated[key] = val
    if updated.get("AGY_SERVER_ADDRESS") and not updated.get("ANTIGRAVITY_LS_ADDRESS"):
        updated["ANTIGRAVITY_LS_ADDRESS"] = updated["AGY_SERVER_ADDRESS"]
    elif updated.get("ANTIGRAVITY_LS_ADDRESS") and not updated.get("AGY_SERVER_ADDRESS"):
        updated["AGY_SERVER_ADDRESS"] = updated["ANTIGRAVITY_LS_ADDRESS"]
    if updated.get("AGY_CSRF_TOKEN") and not updated.get("ANTIGRAVITY_CSRF_TOKEN"):
        updated["ANTIGRAVITY_CSRF_TOKEN"] = updated["AGY_CSRF_TOKEN"]
    elif updated.get("ANTIGRAVITY_CSRF_TOKEN") and not updated.get("AGY_CSRF_TOKEN"):
        updated["AGY_CSRF_TOKEN"] = updated["ANTIGRAVITY_CSRF_TOKEN"]
    if isinstance(payload, Mapping):
        model_name = str(payload.get("modelName") or "").strip()
        if model_name:
            updated["model_name"] = model_name
        conv_id = str(payload.get("conversationId") or "").strip()
        if conv_id:
            updated["last_conversation_id"] = conv_id
    if updated != existing:
        updated["updated_at"] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".agy-runtime-",
                delete=False,
            ) as handle:
                tmp = Path(handle.name)
                json.dump(updated, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return updated


def load_runtime_env(data: Path | None = None) -> dict[str, Any]:
    path = _runtime_env_path(data)
    stored: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, ValueError):
            stored = {}
    for key in (
        "ANTIGRAVITY_LS_ADDRESS",
        "ANTIGRAVITY_CSRF_TOKEN",
        "ANTIGRAVITY_AGENTAPI_EXE",
        "ANTIGRAVITY_APP_DATA_DIR",
        "ANTIGRAVITY_LS_VERSION",
        "ANTIGRAVITY_PROJECT_ID",
        "AGY_SERVER_ADDRESS",
        "AGY_CSRF_TOKEN",
    ):
        env_val = os.environ.get(key, "").strip()
        if env_val:
            stored[key] = env_val
    if stored.get("AGY_SERVER_ADDRESS") and not stored.get("ANTIGRAVITY_LS_ADDRESS"):
        stored["ANTIGRAVITY_LS_ADDRESS"] = stored["AGY_SERVER_ADDRESS"]
    elif stored.get("ANTIGRAVITY_LS_ADDRESS") and not stored.get("AGY_SERVER_ADDRESS"):
        stored["AGY_SERVER_ADDRESS"] = stored["ANTIGRAVITY_LS_ADDRESS"]
    if stored.get("AGY_CSRF_TOKEN") and not stored.get("ANTIGRAVITY_CSRF_TOKEN"):
        stored["ANTIGRAVITY_CSRF_TOKEN"] = stored["AGY_CSRF_TOKEN"]
    elif stored.get("ANTIGRAVITY_CSRF_TOKEN") and not stored.get("AGY_CSRF_TOKEN"):
        stored["AGY_CSRF_TOKEN"] = stored["ANTIGRAVITY_CSRF_TOKEN"]
    return stored


def locate_agentapi(home: Path | None = None, data: Path | None = None) -> Path | None:
    runtime = load_runtime_env(data)
    explicit = str(runtime.get("ANTIGRAVITY_AGENTAPI_EXE") or "").strip()
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()
    root = home or antigravity_home()
    names = ("agentapi.bat", "agentapi.exe", "agentapi") if os.name == "nt" else ("agentapi",)
    for base in (root / "bin", Path.home() / ".gemini" / "antigravity" / "bin"):
        for name in names:
            candidate = base / name
            if candidate.is_file():
                if candidate.suffix.lower() in {".bat", ".cmd"}:
                    try:
                        bat_text = candidate.read_text(encoding="utf-8", errors="replace")
                        match = re.search(r'"([^"]+language_server[^"]*\.exe)"\s+agentapi', bat_text, re.I)
                        if match and Path(match.group(1)).is_file():
                            return Path(match.group(1)).resolve()
                    except OSError:
                        pass
                return candidate.resolve()
    return None


def run_agentapi(
    subcommand: str,
    args: list[str],
    *,
    home: Path | None = None,
    data: Path | None = None,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    exe = locate_agentapi(home, data)
    if exe is None:
        raise FileNotFoundError(_t("未找到 Antigravity agentapi 工具（~/.gemini/antigravity/bin/agentapi）。"))
    runtime = load_runtime_env(data)
    ls_address = str(runtime.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    if not ls_address and data is None:
        live_pairs = _discover_live_language_server()
        if live_pairs:
            addr, csrf = live_pairs[0]
            runtime = save_runtime_env(
                {"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": csrf},
                data=data,
            )
            ls_address = addr
    if not ls_address:
        raise RuntimeError(
            _t("尚未捕获 Antigravity Language Server 连接信息。请先在 Antigravity 中开启或继续任意对话以触发一次 Hook。")
        )

    def _exec(rt: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for key in (
            "ANTIGRAVITY_LS_ADDRESS",
            "ANTIGRAVITY_CSRF_TOKEN",
            "ANTIGRAVITY_AGENTAPI_EXE",
            "ANTIGRAVITY_APP_DATA_DIR",
            "ANTIGRAVITY_LS_VERSION",
            "ANTIGRAVITY_PROJECT_ID",
            "AGY_SERVER_ADDRESS",
            "AGY_CSRF_TOKEN",
        ):
            if rt.get(key):
                env[key] = str(rt[key])
        if "language_server" in exe.stem.lower():
            cmd = [str(exe), "agentapi", subcommand, *args]
        else:
            cmd = [str(exe), subcommand, *args]
        work_dir = cwd if cwd and Path(cwd).is_dir() else str(Path.home())
        return subprocess.run(
            cmd,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **spawn_options(),
        )

    completed = _exec(runtime)
    if completed.returncode != 0 and data is None:
        for addr, csrf in _discover_live_language_server():
            if addr != ls_address:
                runtime = save_runtime_env(
                    {"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": csrf},
                    data=data,
                )
                completed = _exec(runtime)
                if completed.returncode == 0:
                    break
    stdout = (completed.stdout or "").strip()
    if stdout:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        if parsed.get("error"):
                            raise RuntimeError(str(parsed["error"]))
                        return parsed
                except ValueError:
                    pass
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    raise RuntimeError(str(parsed["error"]))
                return parsed
        except ValueError:
            pass
    if completed.returncode != 0:
        err = (completed.stderr or stdout or f"exit {completed.returncode}").strip()
        raise RuntimeError(f"agentapi {subcommand} failed: {err}")
    return {"response": {}}


def _control_queue_path(data: Path | None = None) -> Path:
    return (data or ensure_data_dir()) / "antigravity_control.json"


def queue_pending_control(
    thread_id: str,
    *,
    stop: bool = False,
    inject_message: str | None = None,
    data: Path | None = None,
) -> None:
    if not thread_id:
        return
    path = _control_queue_path(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    records: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                records = loaded
        except (OSError, ValueError):
            records = {}
    entry = records.get(thread_id)
    if not isinstance(entry, dict):
        entry = {"stop": False, "messages": [], "updated_at": time.time()}
    if stop:
        entry["stop"] = True
    if inject_message and inject_message.strip():
        msgs = entry.get("messages")
        if not isinstance(msgs, list):
            msgs = []
        msgs.append(inject_message.strip())
        entry["messages"] = msgs
    entry["updated_at"] = time.time()
    records[thread_id] = entry
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".agy-ctrl-",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            json.dump(records, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def consume_pending_control(
    thread_id: str,
    *,
    data: Path | None = None,
) -> dict[str, Any]:
    if not thread_id:
        return {}
    path = _control_queue_path(data)
    if not path.is_file():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(records, dict) or thread_id not in records:
        return {}
    entry = records.pop(thread_id, None)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".agy-ctrl-",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            json.dump(records, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if not isinstance(entry, dict):
        return {}
    if time.time() - float(entry.get("updated_at") or 0) > 3600:
        return {}
    response: dict[str, Any] = {}
    if entry.get("stop"):
        response["terminationBehavior"] = "terminate"
        return response
    msgs = entry.get("messages")
    if isinstance(msgs, list) and msgs:
        clean_msgs = [str(m) for m in msgs if str(m).strip()]
        if clean_msgs:
            response["injectSteps"] = [{"userMessage": m} for m in clean_msgs]
            response["terminationBehavior"] = "force_continue"
            response["additionalContext"] = "\n\n".join(clean_msgs)
    return response


def normalize_antigravity_hook_payload(
    payload: dict[str, Any],
    *,
    home: Path | None = None,
    data: Path | None = None,
) -> dict[str, Any]:
    """Normalize an Antigravity camelCase hook payload into the unified bridge hook format."""
    if not isinstance(payload, dict) or "conversationId" not in payload:
        return payload
    root = home or antigravity_home()
    runtime_env = save_runtime_env(os.environ, payload, data=data)
    conv_id = str(payload.get("conversationId") or "").strip()
    if not conv_id:
        return payload

    event_name = str(payload.get("hook_event_name") or payload.get("hookEventName") or "").strip()
    if not event_name:
        if "terminationReason" in payload:
            event_name = "Stop"
        elif "toolCall" in payload:
            event_name = "PreToolUse"
        elif "invocationNum" in payload or "initialNumSteps" in payload or "executionNum" in payload:
            event_name = "PostInvocation"
        else:
            event_name = "SessionStart"

    summary = read_conversation_summary(conv_id, root) or {}
    is_subagent = bool(summary.get("isSubagent"))
    if event_name == "Stop" and is_subagent:
        event_name = "SubagentStop"

    thread = build_thread_from_antigravity(conv_id, home=root, include_turns=True) or {}
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    last_turn = turns[-1] if turns and isinstance(turns[-1], dict) else {}
    turn_id = str(
        payload.get("turn_id")
        or payload.get("turnId")
        or last_turn.get("id")
        or f"turn-{payload.get('executionNum', 0)}"
    )

    last_msg = payload.get("last_assistant_message") or payload.get("last-assistant-message")
    if not isinstance(last_msg, str) or not last_msg.strip():
        last_msg = ""
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            for item in reversed(turn.get("items") or []):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and not item.get("questions")
                    and item.get("text")
                ):
                    last_msg = str(item["text"])
                    break
            if last_msg:
                break

    workspace_paths = payload.get("workspacePaths")
    cwd = ""
    if isinstance(workspace_paths, list) and workspace_paths:
        cwd = parse_workspace_uris(workspace_paths)
    if not cwd:
        cwd = str(payload.get("cwd") or summary.get("cwd") or thread.get("cwd") or "")

    model_name = str(
        payload.get("modelName")
        or payload.get("model")
        or summary.get("model")
        or runtime_env.get("model_name")
        or "pro (Gemini 2.5 Pro)"
    )

    normalized: dict[str, Any] = {
        **payload,
        "hook_event_name": event_name,
        "thread_id": conv_id,
        "session_id": conv_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "model": model_name,
        "last_assistant_message": last_msg or "",
        "_antigravity_hook": True,
    }
    if is_subagent:
        normalized["agent_id"] = conv_id
        normalized["sourceKind"] = "subagent"

    t_path = payload.get("transcriptPath") or payload.get("transcript_path")
    if isinstance(t_path, str) and t_path.strip():
        normalized["transcript_path"] = t_path.strip()
    else:
        default_t = transcript_path(conv_id, root)
        if default_t.is_file():
            normalized["transcript_path"] = str(default_t)

    if event_name == "PreToolUse":
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict):
            tc_name = str(tool_call.get("name") or "")
            raw_tc_args = tool_call.get("args") if tool_call.get("args") is not None else tool_call.get("arguments")
            tc_args = _parse_json_dict(raw_tc_args)
            if tc_name == "ask_question":
                normalized["tool_name"] = "request_user_input"
                raw_qs = _parse_json_list(tc_args.get("questions"))
                questions = []
                for idx, q in enumerate(raw_qs, 1):
                    if not isinstance(q, dict):
                        continue
                    questions.append(
                        {
                            "header": str(q.get("header") or f"问题 {idx}"),
                            "question": str(q.get("question") or q.get("title") or ""),
                            "options": [
                                {"label": str(opt)} if not isinstance(opt, dict) else opt
                                for opt in (q.get("options") or [])
                            ],
                        }
                    )
                normalized["tool_input"] = {"questions": questions}
                try:
                    from ..features.async_inputs import hint_async_thread
                    hint_async_thread(conv_id, data or data_dir())
                except Exception:
                    pass
            else:
                normalized.setdefault("tool_name", tc_name)
                normalized.setdefault("tool_input", tc_args)

    return normalized
