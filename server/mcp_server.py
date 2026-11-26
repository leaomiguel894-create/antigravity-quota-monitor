#!/usr/bin/env python3
"""
Antigravity Quota & Context Monitor Server v2.1 (Zero external dependencies).
Supports:
1. Real-time model quota pools, reset countdowns & pace/runway diagnostics
2. Active conversation context window usage, cache hit ratio & token breakdown
3. Model generation speed (tok/s), step latency & historical speed series
4. Both MCP Apps (`aux_pane` / `global` / `inline`) and Native Sidecar HTTP UI
"""

import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

PROTOCOL_VERSION = "2025-03-26"
APP_URI = "ui://quota-monitor/app.html"
SERVER_INFO = {
    "name": "quota-monitor",
    "version": "2.1.0",
}

SPRINT_WINDOW_SECONDS = 5 * 3600
WEEKLY_WINDOW_SECONDS = 7 * 86400

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"

_CACHED_ENDPOINT: Optional[tuple[str, str]] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Antigravity Quota & Context Monitor Server")
    parser.add_argument(
        "--mode",
        choices=["mcp", "sidecar"],
        default="mcp",
        help="Run as stdio MCP server or HTTP sidecar server",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get(
            "QUOTA_MONITOR_DATA_DIR",
            os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", str(BASE_DIR / "data")),
        ),
        help="Directory for persisting quota history",
    )
    return parser.parse_args()


def iter_candidate_endpoints() -> List[tuple[str, str]]:
    candidates: List[tuple[str, str]] = []
    seen = set()

    def add(addr: str, token: str) -> None:
        key = (addr, token)
        if addr and key not in seen:
            seen.add(key)
            candidates.append(key)

    if _CACHED_ENDPOINT:
        add(*_CACHED_ENDPOINT)

    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    env_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if env_addr:
        add(env_addr, env_token)

    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    if daemon_dir.is_dir():
        files = sorted(
            daemon_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for fp in files[:15]:
            try:
                info = json.loads(fp.read_text(encoding="utf-8"))
                port = info.get("httpPort")
                token = str(info.get("csrfToken") or "").strip()
                if port:
                    add(f"127.0.0.1:{port}", token)
            except Exception:
                continue

    add("127.0.0.1:5387", env_token)
    return candidates


def call_ls_rpc(service_path: str, payload: Dict[str, Any], connect_headers: bool = False) -> Dict[str, Any]:
    global _CACHED_ENDPOINT
    last_err: Optional[Exception] = None
    body_bytes = json.dumps(payload).encode("utf-8")

    for addr, token in iter_candidate_endpoints():
        url = f"http://{addr}/{service_path.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        if connect_headers:
            headers["Connect-Protocol-Version"] = "1"
            if token:
                headers["x-antigravity-csrf-token"] = token
                headers["x-codeium-csrf-token"] = token
        else:
            if token:
                headers["x-codeium-csrf-token"] = token

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                _CACHED_ENDPOINT = (addr, token)
                return data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
            last_err = RuntimeError(f"HTTP {e.code} ({addr}): {err_body}")
        except Exception as e:
            last_err = e

    raise RuntimeError(f"无法连接本地 Antigravity 服务: {last_err}")


def fetch_user_status() -> Dict[str, Any]:
    return call_ls_rpc("exa.language_server_pb.LanguageServerService/GetUserStatus", {})


def fetch_quota_summary() -> Optional[Dict[str, Any]]:
    try:
        return call_ls_rpc("exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary", {})
    except Exception:
        return None


def parse_iso_ts(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def infer_context_limit(model_id_or_label: str) -> int:
    low = (model_id_or_label or "").lower()
    if "claude" in low:
        return 200_000
    if "gpt" in low or "openai" in low:
        return 128_000
    # Gemini 3.x / 4.x models support 1M context window
    return 1_000_000


def count_tool_calls_in_field(raw_tc: Any) -> int:
    if not raw_tc:
        return 0
    if isinstance(raw_tc, list):
        return len(raw_tc)
    if isinstance(raw_tc, str):
        s = raw_tc.strip()
        if not s:
            return 0
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                return len(parsed)
        except Exception:
            pass
        # Fallback count by `'name':` occurrences
        c = s.count("'name':") or s.count('"name":')
        return max(1, c)
    return 0


def analyze_session_context_and_speed(
    model_label_map: Dict[str, str],
    target_conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
    recent_sessions: List[Dict[str, Any]] = []

    # 1. Try official ConversationService/ListConversations for rich titles & runState
    try:
        conv_resp = call_ls_rpc(
            "product.v1.ConversationService/ListConversations",
            {"pageSize": 8, "includeArchived": False},
            connect_headers=True,
        )
        for c in conv_resp.get("conversations", []) or []:
            cid = c.get("id")
            if not cid:
                continue
            ann = c.get("annotations") or {}
            st = c.get("state") or {}
            title = ann.get("title") or st.get("summary") or cid[:8]
            recent_sessions.append({
                "id": cid,
                "title": title.strip()[:60],
                "runState": st.get("runState") or "RUN_STATE_IDLE",
                "updateTime": st.get("updateTime") or (c.get("metadata") or {}).get("createTime") or "",
            })
    except Exception:
        pass

    # 2. Fallback or supplement from on-disk brain/<id>/.system_generated/logs/transcript.jsonl
    if not recent_sessions and brain_dir.is_dir():
        candidates = []
        for child in brain_dir.iterdir():
            tp = child / ".system_generated" / "logs" / "transcript.jsonl"
            if tp.is_file():
                candidates.append((tp.stat().st_mtime, child.name, tp))
        candidates.sort(reverse=True)
        for mtime, cid, _ in candidates[:8]:
            recent_sessions.append({
                "id": cid,
                "title": f"会话 {cid[:8]}",
                "runState": "RUN_STATE_IDLE",
                "updateTime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            })

    if not recent_sessions:
        return {"available": False, "recentSessions": []}

    chosen = None
    if target_conversation_id:
        for s in recent_sessions:
            if s["id"] == target_conversation_id:
                chosen = s
                break
    if not chosen:
        # Prefer a session that actually has a transcript file on disk
        for s in recent_sessions:
            tp = brain_dir / s["id"] / ".system_generated" / "logs" / "transcript.jsonl"
            if tp.is_file():
                chosen = s
                break
        if not chosen:
            chosen = recent_sessions[0]

    cid = chosen["id"]
    tp = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not tp.is_file():
        return {
            "available": False,
            "conversationId": cid,
            "title": chosen["title"],
            "runState": chosen["runState"],
            "recentSessions": recent_sessions,
        }

    file_mtime = tp.stat().st_mtime
    raw_lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
    events: List[Dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue

    user_turns = 0
    model_steps = 0
    tool_calls_total = 0
    total_fresh_input = 0
    total_cache_read = 0
    total_output = 0

    latest_pr: Optional[Dict[str, Any]] = None
    speed_samples: List[Dict[str, Any]] = []

    for idx, ev in enumerate(events):
        ev_type = ev.get("type")
        if ev_type == "USER_INPUT":
            user_turns += 1
            continue

        if ev_type != "PLANNER_RESPONSE":
            continue

        model_steps += 1
        inp = int(ev.get("input_tokens") or 0)
        cache_tok = int(ev.get("cache_read_tokens") or 0)
        out_tok = int(ev.get("output_tokens") or 0)
        total_fresh_input += inp
        total_cache_read += cache_tok
        total_output += out_tok
        tool_calls_total += count_tool_calls_in_field(ev.get("tool_calls"))
        latest_pr = ev

        # Speed measurement: determine accurate generation completion timestamp
        t_start = parse_iso_ts(ev.get("created_at"))
        t_end: Optional[float] = None
        if t_start is not None and out_tok > 0:
            if idx + 1 < len(events):
                nxt = events[idx + 1]
                # Only use next event timestamp if it's an immediate system/tool step (not user idle time!)
                if nxt.get("type") in ("GENERIC", "PLANNER_RESPONSE"):
                    t_end = parse_iso_ts(nxt.get("created_at"))
                    # When streaming parallel tool calls, the last tool's `Created At:` marks true stream end
                    if nxt.get("type") == "GENERIC" and isinstance(nxt.get("content"), str):
                        created_matches = re.findall(r"Created At:\s*(\S+)", nxt["content"])
                        for cm in created_matches:
                            ts_cm = parse_iso_ts(cm)
                            if ts_cm is not None and (t_end is None or ts_cm > t_end):
                                t_end = ts_cm
            elif ev.get("status") == "DONE":
                # Last event in transcript: file mtime reflects exact completion write
                if file_mtime >= t_start:
                    t_end = file_mtime

        if t_start is not None and t_end is not None:
            dt = t_end - t_start
            # Filter realistic generation window (0.5s .. 600s)
            if 0.5 <= dt <= 600.0:
                tps = round(out_tok / dt, 1)
                raw_model = str(ev.get("model") or "")
                human_model = model_label_map.get(raw_model.lower(), raw_model or "Model")
                speed_samples.append({
                    "stepIndex": ev.get("step_index", idx),
                    "model": human_model,
                    "outputTokens": out_tok,
                    "durationSec": round(dt, 2),
                    "tokPerSec": tps,
                    "createdAt": ev.get("created_at") or "",
                })

    # Active context metrics from latest PLANNER_RESPONSE
    if latest_pr:
        cur_fresh = int(latest_pr.get("input_tokens") or 0)
        cur_cache = int(latest_pr.get("cache_read_tokens") or 0)
        cur_out = int(latest_pr.get("output_tokens") or 0)
        cur_prompt_total = cur_fresh + cur_cache
        active_context_tokens = cur_prompt_total + cur_out
        cache_hit_pct = round((cur_cache / cur_prompt_total) * 100.0, 1) if cur_prompt_total > 0 else 0.0
        raw_cur_model = str(latest_pr.get("model") or "")
        cur_model_label = model_label_map.get(raw_cur_model.lower(), raw_cur_model or "Unknown")
    else:
        cur_fresh = 0
        cur_cache = 0
        cur_out = 0
        active_context_tokens = 0
        cache_hit_pct = 0.0
        raw_cur_model = ""
        cur_model_label = "暂无模型调用"

    auto_limit = infer_context_limit(f"{raw_cur_model} {cur_model_label}")
    ctx_pct = round(min(100.0, (active_context_tokens / auto_limit) * 100.0), 2) if auto_limit > 0 else 0.0

    # Speed summary
    if speed_samples:
        latest_sample = speed_samples[-1]
        latest_tps = latest_sample["tokPerSec"]
        latest_dur = latest_sample["durationSec"]
        latest_out = latest_sample["outputTokens"]
        avg_tps = round(sum(s["tokPerSec"] for s in speed_samples) / len(speed_samples), 1)
        peak_tps = round(max(s["tokPerSec"] for s in speed_samples), 1)
    else:
        latest_tps = None
        latest_dur = None
        latest_out = cur_out if cur_out > 0 else None
        avg_tps = None
        peak_tps = None

    return {
        "available": True,
        "conversationId": cid,
        "title": chosen["title"],
        "runState": chosen["runState"],
        "updateTime": chosen.get("updateTime") or "",
        "currentModel": cur_model_label,
        "context": {
            "activeTokens": active_context_tokens,
            "autoLimitTokens": auto_limit,
            "usagePercent": ctx_pct,
            "remainingTokens": max(0, auto_limit - active_context_tokens),
            "freshInputTokens": cur_fresh,
            "cacheReadTokens": cur_cache,
            "latestOutputTokens": cur_out,
            "cacheHitPercent": cache_hit_pct,
        },
        "totals": {
            "userTurns": user_turns,
            "modelSteps": model_steps,
            "toolCalls": tool_calls_total,
            "totalFreshInputTokens": total_fresh_input,
            "totalCacheReadTokens": total_cache_read,
            "totalOutputTokens": total_output,
        },
        "speed": {
            "latestTokPerSec": latest_tps,
            "avgTokPerSec": avg_tps,
            "peakTokPerSec": peak_tps,
            "latestDurationSec": latest_dur,
            "latestOutputTokens": latest_out,
            "sampleCount": len(speed_samples),
            "series": speed_samples[-14:],
        },
        "recentSessions": recent_sessions,
    }


def classify_family(label: str) -> str:
    low = label.lower()
    if "claude" in low:
        return "Claude"
    if "gpt" in low or "openai" in low or "o1" in low or "o3" in low:
        return "GPT / OpenAI"
    if "orchestrator" in low:
        return "Orchestrator"
    if "gemini" in low:
        if "flash" in low:
            return "Gemini Flash"
        return "Gemini"
    return "其他模型"


def compute_pace_metrics(remaining_pct: float, reset_time_iso: str, explicit_window: str = "") -> Dict[str, Any]:
    used_pct = max(0.0, min(100.0, 100.0 - remaining_pct))
    if not reset_time_iso:
        return {
            "windowType": explicit_window or "unknown",
            "windowLabel": "未知周期",
            "secondsRemaining": None,
            "timeRemainingPercent": None,
            "elapsedPercent": None,
            "paceDelta": 0.0,
            "paceStatus": "steady",
            "paceLabel": "节奏平稳",
            "burnRatePerHour": None,
            "estimatedRunwayHours": None,
        }

    try:
        reset_dt = datetime.fromisoformat(reset_time_iso.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        secs_left = max(0.0, (reset_dt - now_dt).total_seconds())
    except Exception:
        return {
            "windowType": explicit_window or "unknown",
            "windowLabel": "未知周期",
            "secondsRemaining": None,
            "timeRemainingPercent": None,
            "elapsedPercent": None,
            "paceDelta": 0.0,
            "paceStatus": "steady",
            "paceLabel": "节奏平稳",
            "burnRatePerHour": None,
            "estimatedRunwayHours": None,
        }

    win_low = (explicit_window or "").lower()
    if win_low == "5h" or (not win_low and secs_left <= SPRINT_WINDOW_SECONDS + 900):
        total_window = SPRINT_WINDOW_SECONDS
        window_type = "5h"
        window_label = "5 小时限额"
    else:
        total_window = WEEKLY_WINDOW_SECONDS
        window_type = "weekly"
        window_label = "每周限额"

    time_rem_pct = round(max(0.0, min(100.0, (secs_left / total_window) * 100.0)), 1)
    elapsed_pct = round(100.0 - time_rem_pct, 1)
    elapsed_hours = max(0.05, (total_window - secs_left) / 3600.0)
    pace_delta = round(remaining_pct - time_rem_pct, 1)

    burn_rate = round(used_pct / elapsed_hours, 2) if used_pct > 0.2 and elapsed_pct >= 2.0 else 0.0
    runway_hours = round(remaining_pct / burn_rate, 1) if burn_rate > 0.1 else None

    if remaining_pct >= 99.5 or used_pct < 1.0:
        pace_status = "idle"
        pace_label = "满额待命"
    elif pace_delta >= 12.0:
        pace_status = "surplus"
        pace_label = "余量充裕"
    elif pace_delta <= -15.0:
        pace_status = "fast"
        pace_label = "消耗偏快"
    else:
        pace_status = "steady"
        pace_label = "节奏均衡"

    return {
        "windowType": window_type,
        "windowLabel": window_label,
        "secondsRemaining": int(secs_left),
        "timeRemainingPercent": time_rem_pct,
        "elapsedPercent": elapsed_pct,
        "paceDelta": pace_delta,
        "paceStatus": pace_status,
        "paceLabel": pace_label,
        "burnRatePerHour": burn_rate,
        "estimatedRunwayHours": runway_hours,
    }


def map_group_display_name_cn(raw_name: str) -> tuple[str, str]:
    low = (raw_name or "").lower()
    if "gemini" in low:
        return "Gemini 模型组", "Gemini"
    if "claude" in low or "gpt" in low:
        return "Claude & GPT 模型组", "Claude / GPT"
    return raw_name or "其他模型组", "其他模型"


def normalize_quota_data(
    raw: Dict[str, Any],
    quota_summary_raw: Optional[Dict[str, Any]] = None,
    target_conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    us = raw.get("userStatus", {}) or {}
    plan_status = us.get("planStatus", {}) or {}
    plan_info = plan_status.get("planInfo", {}) or {}
    user_tier = us.get("userTier", {}) or {}
    models_raw = (us.get("cascadeModelConfigData", {}) or {}).get("clientModelConfigs", []) or []

    all_models: List[Dict[str, Any]] = []
    model_label_map: Dict[str, str] = {}
    models_by_bucket: Dict[str, List[Dict[str, Any]]] = {"Gemini": [], "Claude / GPT": [], "其他模型": []}

    for m in models_raw:
        label = m.get("label") or "Unknown Model"
        mid = m.get("modelId") or ""
        placeholder = (m.get("modelOrAlias") or {}).get("model") or ""
        if mid:
            model_label_map[mid.lower()] = label
        if placeholder:
            model_label_map[placeholder.lower()] = label

        qi = m.get("quotaInfo") or {}
        tag = m.get("tagTitle") or ""
        tag_desc = m.get("tagDescription") or ""
        rem = qi.get("remainingFraction")
        rem_pct = round(max(0.0, min(1.0, float(rem if rem is not None else 1.0))) * 100.0, 2)
        reset_time = qi.get("resetTime") or ""
        family = classify_family(label)

        bucket_key = "Claude / GPT" if family in ("Claude", "GPT / OpenAI") else ("其他模型" if family == "其他模型" else "Gemini")
        model_entry = {
            "label": label,
            "modelId": mid or placeholder,
            "family": family,
            "bucket": bucket_key,
            "tagTitle": tag,
            "tagDescription": tag_desc,
            "remainingPercent": rem_pct,
            "displayPercent": int(round(rem_pct)),
            "resetTime": reset_time,
        }
        all_models.append(model_entry)
        models_by_bucket.setdefault(bucket_key, []).append(model_entry)

    # Analyze current session first so we know which model group is currently active!
    session_stats = analyze_session_context_and_speed(model_label_map, target_conversation_id)
    cur_model_name = session_stats.get("currentModel") or ""
    cur_family = classify_family(cur_model_name)
    active_bucket_key = "Claude / GPT" if cur_family in ("Claude", "GPT / OpenAI") else "Gemini"

    pools: List[Dict[str, Any]] = []
    qs_groups = ((quota_summary_raw or {}).get("response") or {}).get("groups") or []

    if qs_groups:
        # Authoritative dual-window quota from RetrieveUserQuotaSummary (matches official View Usage 100%)
        for idx, g in enumerate(qs_groups):
            raw_gname = g.get("displayName") or f"Group {idx + 1}"
            cn_name, bucket_key = map_group_display_name_cn(raw_gname)
            raw_buckets = g.get("buckets") or []

            win_5h: Optional[Dict[str, Any]] = None
            win_weekly: Optional[Dict[str, Any]] = None
            parsed_windows: List[Dict[str, Any]] = []

            for b in raw_buckets:
                w_type = str(b.get("window") or "").lower()
                frac = float(b.get("remainingFraction") if b.get("remainingFraction") is not None else 0.0)
                exact_pct = round(max(0.0, min(1.0, frac)) * 100.0, 2)
                disp_pct = int(round(exact_pct))  # Matches View Usage integer rounding!
                r_time = b.get("resetTime") or ""
                pace_obj = compute_pace_metrics(exact_pct, r_time, w_type)
                w_entry = {
                    "bucketId": b.get("bucketId") or w_type,
                    "window": "5h" if w_type == "5h" else "weekly",
                    "label": "5 小时限额 (5h)" if w_type == "5h" else "每周限额 (Weekly)",
                    "shortLabel": "5h 限额" if w_type == "5h" else "周限额",
                    "remainingPercent": exact_pct,
                    "displayPercent": disp_pct,
                    "resetTime": r_time,
                    "description": b.get("description") or "",
                    "pace": pace_obj,
                }
                parsed_windows.append(w_entry)
                if w_type == "5h":
                    win_5h = w_entry
                elif w_type == "weekly":
                    win_weekly = w_entry

            # Order windows: 5h first (active sprint bottleneck), weekly second (baseline cap)
            parsed_windows.sort(key=lambda w: 0 if w["window"] == "5h" else 1)

            primary_win = win_5h or win_weekly or (parsed_windows[0] if parsed_windows else None)
            effective_pct = min((w["remainingPercent"] for w in parsed_windows), default=100.0)
            effective_disp = min((w["displayPercent"] for w in parsed_windows), default=100)

            # Sync per-model display quota with authoritative group effective / 5h quota
            group_models = models_by_bucket.get(bucket_key, [])
            for gm in group_models:
                gm["remainingPercent"] = win_5h["remainingPercent"] if win_5h else effective_pct
                gm["displayPercent"] = win_5h["displayPercent"] if win_5h else effective_disp
                gm["weeklyPercent"] = win_weekly["displayPercent"] if win_weekly else None
                if primary_win and primary_win["resetTime"]:
                    gm["resetTime"] = primary_win["resetTime"]

            pools.append({
                "id": f"group_{bucket_key}",
                "name": cn_name,
                "officialName": raw_gname,
                "bucket": bucket_key,
                "isCurrentSessionPool": (bucket_key == active_bucket_key),
                "remainingPercent": win_5h["remainingPercent"] if win_5h else effective_pct,
                "displayPercent": win_5h["displayPercent"] if win_5h else effective_disp,
                "weeklyRemainingPercent": win_weekly["remainingPercent"] if win_weekly else None,
                "weeklyDisplayPercent": win_weekly["displayPercent"] if win_weekly else None,
                "effectiveRemainingPercent": effective_pct,
                "effectiveDisplayPercent": effective_disp,
                "resetTime": primary_win["resetTime"] if primary_win else "",
                "weeklyResetTime": win_weekly["resetTime"] if win_weekly else "",
                "pace": primary_win["pace"] if primary_win else compute_pace_metrics(effective_pct, ""),
                "windows": parsed_windows,
                "models": group_models,
            })
    else:
        # Fallback to GetUserStatus if RetrieveUserQuotaSummary is unavailable
        pools_map: Dict[str, Dict[str, Any]] = {}
        for m in all_models:
            bucket_key = m["bucket"]
            pool_key = f"{bucket_key}|{m['resetTime']}"
            if pool_key not in pools_map:
                pace_obj = compute_pace_metrics(m["remainingPercent"], m["resetTime"])
                pools_map[pool_key] = {
                    "id": pool_key,
                    "name": f"{bucket_key} 模型组",
                    "officialName": bucket_key,
                    "bucket": bucket_key,
                    "isCurrentSessionPool": (bucket_key == active_bucket_key),
                    "remainingPercent": m["remainingPercent"],
                    "displayPercent": m["displayPercent"],
                    "weeklyRemainingPercent": None,
                    "weeklyDisplayPercent": None,
                    "effectiveRemainingPercent": m["remainingPercent"],
                    "effectiveDisplayPercent": m["displayPercent"],
                    "resetTime": m["resetTime"],
                    "weeklyResetTime": "",
                    "pace": pace_obj,
                    "windows": [
                        {
                            "bucketId": "default",
                            "window": pace_obj["windowType"],
                            "label": pace_obj["windowLabel"],
                            "shortLabel": "当前窗口",
                            "remainingPercent": m["remainingPercent"],
                            "displayPercent": m["displayPercent"],
                            "resetTime": m["resetTime"],
                            "description": "",
                            "pace": pace_obj,
                        }
                    ],
                    "models": [],
                }
            pools_map[pool_key]["models"].append(m)
        pools = list(pools_map.values())

    # Sort so active session's model pool is first by default, or keep Gemini -> Claude order
    pools.sort(key=lambda p: (0 if p.get("isCurrentSessionPool") else 1, 0 if p["bucket"] == "Gemini" else 1))

    lowest_pct = min((p["effectiveRemainingPercent"] for p in pools), default=100.0)
    lowest_disp = min((p["effectiveDisplayPercent"] for p in pools), default=100)
    avg_pct = round(sum(p["effectiveRemainingPercent"] for p in pools) / len(pools), 2) if pools else 100.0

    # Nearest reset across all 5h/weekly windows
    all_resets = [
        w["resetTime"]
        for p in pools
        for w in p.get("windows", [])
        if w.get("resetTime")
    ]
    all_resets_sorted = sorted(all_resets, key=lambda iso: parse_iso_ts(iso) or 9e18)
    nearest_reset = all_resets_sorted[0] if all_resets_sorted else (pools[0]["resetTime"] if pools else "")

    alerts: List[Dict[str, str]] = []
    for p in pools:
        for w in p.get("windows", []):
            if w["displayPercent"] <= 20:
                alerts.append({
                    "level": "danger",
                    "text": f"{p['name']} 的{w['shortLabel']}仅剩 {w['displayPercent']}%，请留意重置时间或切换模型组。",
                })
            elif w["window"] == "5h" and w["pace"]["paceStatus"] == "fast" and w["displayPercent"] < 60:
                alerts.append({
                    "level": "warn",
                    "text": f"{p['name']} 5h 窗口消耗偏快（剩余 {w['displayPercent']}%），预计约 {w['pace']['estimatedRunwayHours'] or '--'}h 触顶。",
                })

    account = {
        "name": us.get("name") or "",
        "email": us.get("email") or "",
        "tierName": user_tier.get("name") or plan_info.get("planName") or "Standard",
        "planName": plan_info.get("planName") or "",
        "teamsTier": plan_info.get("teamsTier") or "",
        "availablePromptCredits": plan_status.get("availablePromptCredits"),
        "monthlyPromptCredits": plan_info.get("monthlyPromptCredits"),
        "availableFlowCredits": plan_status.get("availableFlowCredits"),
        "monthlyFlowCredits": plan_info.get("monthlyFlowCredits"),
        "upgradeUri": user_tier.get("upgradeSubscriptionUri") or "",
    }

    return {
        "ok": True,
        "source": "RetrieveUserQuotaSummary" if qs_groups else "GetUserStatus",
        "fetchedAt": now_iso,
        "account": account,
        "summary": {
            "lowestRemainingPercent": lowest_pct,
            "lowestDisplayPercent": lowest_disp,
            "averageRemainingPercent": avg_pct,
            "nearestResetTime": nearest_reset,
            "poolCount": len(pools),
            "modelCount": len(all_models),
            "activeBucket": active_bucket_key,
        },
        "alerts": alerts,
        "pools": pools,
        "models": all_models,
        "session": session_stats,
    }


class HistoryStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "history.json"
        self.max_points = 240

    def load(self) -> List[Dict[str, Any]]:
        if not self.file_path.exists():
            return []
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def append(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        history = self.load()
        ts = snapshot.get("fetchedAt", datetime.now(timezone.utc).isoformat())
        point = {
            "t": ts,
            "lowest": snapshot.get("summary", {}).get("lowestRemainingPercent", 100.0),
            "pools": {
                p["name"]: p["remainingPercent"]
                for p in snapshot.get("pools", [])
            },
        }

        if history:
            last = history[-1]
            if last.get("pools") == point["pools"]:
                try:
                    t_last = datetime.fromisoformat(last["t"].replace("Z", "+00:00"))
                    t_cur = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if (t_cur - t_last).total_seconds() < 20:
                        return history
                except Exception:
                    pass

        history.append(point)
        if len(history) > self.max_points:
            history = history[-self.max_points :]

        try:
            tmp = self.file_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False)
            tmp.replace(self.file_path)
        except Exception:
            pass
        return history

    def clear(self) -> List[Dict[str, Any]]:
        try:
            if self.file_path.exists():
                self.file_path.unlink()
        except Exception:
            pass
        return []


def build_ui_html(runtime_mode: str = "mcp") -> str:
    html_path = UI_DIR / "app.html"
    html = html_path.read_text(encoding="utf-8")

    if runtime_mode == "mcp":
        sdk_path = UI_DIR / "ext-apps.bundle.js"
        sdk = sdk_path.read_text(encoding="utf-8").strip()
        export_idx = sdk.rfind("export{")
        if export_idx != -1:
            export_stmt = sdk[export_idx:].strip()
            inner = export_stmt[len("export{") :].rstrip(";").rstrip("}")
            pairs = []
            for item in inner.split(","):
                item = item.strip()
                if not item:
                    continue
                if " as " in item:
                    local_name, exported_name = item.split(" as ", 1)
                    pairs.append(f"{exported_name.strip()}:{local_name.strip()}")
                else:
                    pairs.append(f"{item}:{item}")
            sdk = sdk[:export_idx] + "\nconst __EXT_APPS__ = {" + ",".join(pairs) + "};\n"
        sdk_safe = sdk.replace("</script>", "<\\/script>")
        html = html.replace("/*__INLINE_EXT_APPS_SDK__*/", sdk_safe)
        html = html.replace("__RUNTIME_MODE__", "mcp")
    else:
        html = html.replace("/*__INLINE_EXT_APPS_SDK__*/", "const __EXT_APPS__ = null;")
        html = html.replace("__RUNTIME_MODE__", "sidecar")

    return html


def build_summary_text(snapshot: Dict[str, Any]) -> str:
    if not snapshot.get("ok"):
        return f"额度查询失败：{snapshot.get('error', '未知错误')}"
    acc = snapshot.get("account", {})
    lines = [
        f"Antigravity 额度与上下文监控（{acc.get('tierName', 'Unknown')}）",
    ]
    for p in snapshot.get("pools", []):
        cur_mark = " [当前会话]" if p.get("isCurrentSessionPool") else ""
        win_parts = [f"{w['shortLabel']} {w['displayPercent']}% ({w['remainingPercent']:.1f}%)" for w in p.get("windows", [])]
        lines.append(f"- {p['name']}{cur_mark}：{' ｜ '.join(win_parts)}")
    sess = snapshot.get("session") or {}
    if sess.get("available"):
        ctx = sess.get("context") or {}
        spd = sess.get("speed") or {}
        lines.append(
            f"- 当前会话上下文：{ctx.get('activeTokens', 0):,} tokens（缓存命中 {ctx.get('cacheHitPercent', 0)}%）"
        )
        if spd.get("latestTokPerSec") is not None:
            lines.append(
                f"- 输出速度：最新 {spd.get('latestTokPerSec')} tok/s · 平均 {spd.get('avgTokPerSec')} tok/s"
            )
    return "\n".join(lines)


class QuotaService:
    def __init__(self, data_dir: str) -> None:
        self.store = HistoryStore(data_dir)

    def get_snapshot_payload(self, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            raw = fetch_user_status()
            qs = fetch_quota_summary()
            snapshot = normalize_quota_data(raw, qs, conversation_id)
            history = self.store.append(snapshot)
            snapshot["history"] = history
            return snapshot
        except Exception as e:
            return {
                "ok": False,
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
                "history": self.store.load(),
            }

    def clear_history_payload(self, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        self.store.clear()
        return self.get_snapshot_payload(conversation_id)


# ============================================================================
# Mode 1: MCP Server (stdio JSON-RPC 2.0)
# ============================================================================
class QuotaMonitorMCPServer:
    def __init__(self, service: QuotaService) -> None:
        self.service = service

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "open_quota_monitor",
                "title": "额度监控",
                "description": "在右侧辅助面板打开 Antigravity 额度与会话监控（Apple 毛玻璃视觉面板），实时查看模型额度池余量、配速节奏、当前会话上下文占用、缓存命中率与输出速度 (tok/s)。",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "_meta": {
                    "ui": {
                        "resourceUri": APP_URI,
                    },
                    "com.google.antigravity/ui": {
                        "entrypoints": [
                            {"type": "aux_pane", "title": "额度监控"},
                            {"type": "global", "title": "额度监控"},
                        ]
                    },
                },
            },
            {
                "name": "refresh_quota",
                "title": "刷新额度与会话数据",
                "description": "仅供可视化界面内部调用，拉取最新额度、会话上下文及输出速度快照。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "conversationId": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "_meta": {"ui": {"visibility": ["app"]}},
            },
            {
                "name": "clear_quota_history",
                "title": "清空历史趋势",
                "description": "仅供可视化界面内部调用，清空本地保存的历史额度趋势记录。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "conversationId": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "_meta": {"ui": {"visibility": ["app"]}},
            },
        ]

    def list_resources(self) -> List[Dict[str, Any]]:
        return [
            {
                "uri": APP_URI,
                "name": "额度与会话监控面板",
                "description": "Antigravity 实时额度、当前会话上下文与输出速度可视化监控界面（Apple Liquid Glass 风格）",
                "mimeType": "text/html;profile=mcp-app",
                "_meta": {"ui": {"prefersBorder": False}},
            }
        ]

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if req_id is None:
            return None

        try:
            if method == "initialize":
                client_ver = params.get("protocolVersion") or PROTOCOL_VERSION
                return self._ok(
                    req_id,
                    {
                        "protocolVersion": client_ver,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": SERVER_INFO,
                    },
                )
            if method == "ping":
                return self._ok(req_id, {})
            if method == "tools/list":
                return self._ok(req_id, {"tools": self.list_tools()})
            if method == "resources/list":
                return self._ok(req_id, {"resources": self.list_resources()})
            if method == "resources/read":
                uri = params.get("uri")
                if uri != APP_URI:
                    return self._err(req_id, -32602, f"Unknown resource URI: {uri}")
                html = build_ui_html("mcp")
                return self._ok(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": "text/html;profile=mcp-app",
                                "text": html,
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                )
            if method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                conv_id = args.get("conversationId") or None
                if name in ("open_quota_monitor", "refresh_quota"):
                    payload = self.service.get_snapshot_payload(conv_id)
                    return self._ok(
                        req_id,
                        {
                            "content": [{"type": "text", "text": build_summary_text(payload)}],
                            "structuredContent": payload,
                            "isError": False,
                        },
                    )
                if name == "clear_quota_history":
                    payload = self.service.clear_history_payload(conv_id)
                    return self._ok(
                        req_id,
                        {
                            "content": [{"type": "text", "text": "已清空历史趋势记录并重新采样。"}],
                            "structuredContent": payload,
                            "isError": False,
                        },
                    )
                return self._err(req_id, -32602, f"Unknown tool: {name}")

            return self._err(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            return self._err(req_id, -32603, str(e))

    @staticmethod
    def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ============================================================================
# Mode 2: Native Sidecar Web UI Server (SIDECAR_UI_ENTRYPOINT_AUX_PANE)
# ============================================================================
def run_sidecar_http_server(service: QuotaService) -> None:
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "0"))
    ui_token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN", "")
    if not port:
        raise RuntimeError("Missing ANTIGRAVITY_SIDECAR_WEB_PORT in sidecar mode")

    preload_candidates = [
        Path.home() / ".gemini" / "antigravity" / "lib" / "node_modules" / "sidecar_sdk" / "preload.js",
    ]
    preload_js = ""
    for p in preload_candidates:
        if p.is_file():
            preload_js = p.read_text(encoding="utf-8")
            break

    class SidecarHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _check_token(self, parsed) -> bool:
            if not ui_token:
                return True
            hdr = self.headers.get("x-sidecar-token", "")
            qs = parse_qs(parsed.query).get("token", [""])[0]
            return (hdr == ui_token) or (qs == ui_token)

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            conv_id = qs.get("conversationId", [None])[0]
            if parsed.path == "/preload.js":
                self._send(200, "application/javascript; charset=utf-8", preload_js.encode("utf-8"))
                return
            if parsed.path in ("/", "/index.html"):
                html = build_ui_html("sidecar")
                self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            if parsed.path == "/api/quota":
                payload = service.get_snapshot_payload(conv_id)
                self._send(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                return
            self._send(404, "text/plain; charset=utf-8", b"Not Found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._check_token(parsed):
                self._send(401, "application/json; charset=utf-8", b'{"error":"unauthorized"}')
                return
            qs = parse_qs(parsed.query)
            conv_id = qs.get("conversationId", [None])[0]
            if parsed.path == "/api/quota":
                payload = service.get_snapshot_payload(conv_id)
                self._send(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                return
            if parsed.path == "/api/clear-history":
                payload = service.clear_history_payload(conv_id)
                self._send(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                return
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

    httpd = ThreadingHTTPServer(("127.0.0.1", port), SidecarHandler)
    print(f"[quota-monitor sidecar] listening on 127.0.0.1:{port}", flush=True)
    httpd.serve_forever()


def main() -> None:
    args = parse_args()
    service = QuotaService(args.data_dir)

    if args.mode == "sidecar":
        run_sidecar_http_server(service)
        return

    server = QuotaMonitorMCPServer(service)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = server.handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
