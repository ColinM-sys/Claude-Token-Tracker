"""
Claude Token Tracker — Local web dashboard for Claude Code token usage.
Reads session data from ~/.claude/ and serves a web dashboard.

Usage: python server.py
Then open http://localhost:8050 in your browser.
"""

import json
import os
import re
import time
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PORT = 8050
CACHE_TTL_SECONDS = 30
SCRIPT_DIR = Path(__file__).parent

# ── Anthropic API Pricing (per million tokens) ────────────────────────────────
# (input, output, cache_read, cache_5m_write, cache_1h_write)

MODEL_PRICING = {
    "claude-opus-4-6":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-1":   (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":     (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4":   (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-4-5":  (1.00, 5.00, 0.10, 1.25, 2.00),
    "claude-haiku-3-5":  (0.80, 4.00, 0.08, 1.00, 1.60),
    "claude-haiku-3":    (0.25, 1.25, 0.03, 0.30, 0.50),
}
WEB_SEARCH_COST = 0.01  # $0.01 per search


def get_pricing(model_id):
    """Map a model ID like 'claude-opus-4-5-20251101' to its pricing tuple."""
    if not model_id or model_id == "<synthetic>":
        return None
    clean = re.sub(r"-\d{8}$", "", model_id)
    if clean in MODEL_PRICING:
        return MODEL_PRICING[clean]
    for key, pricing in MODEL_PRICING.items():
        if key in clean:
            return pricing
    # Fallback: try to identify by family name
    if "opus" in clean:
        return MODEL_PRICING["claude-opus-4-5"]
    if "sonnet" in clean:
        return MODEL_PRICING["claude-sonnet-4-5"]
    if "haiku" in clean:
        return MODEL_PRICING["claude-haiku-4-5"]
    return MODEL_PRICING["claude-opus-4-5"]  # conservative default


def compute_cost(model_id, usage):
    """Compute the API-equivalent cost for a single request's usage."""
    pricing = get_pricing(model_id)
    if not pricing:
        return 0.0
    input_rate, output_rate, cache_read_rate, cache_5m_rate, cache_1h_rate = pricing

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)

    cache_creation = usage.get("cache_creation", {})
    cache_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
    cache_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
    total_cache_write = usage.get("cache_creation_input_tokens", 0)

    if cache_5m == 0 and cache_1h == 0 and total_cache_write > 0:
        cache_5m = total_cache_write  # treat as 5-min cache (conservative)

    web_searches = usage.get("server_tool_use", {}).get("web_search_requests", 0) if isinstance(usage.get("server_tool_use"), dict) else 0

    cost = (
        (input_tokens / 1_000_000) * input_rate
        + (output_tokens / 1_000_000) * output_rate
        + (cache_read / 1_000_000) * cache_read_rate
        + (cache_5m / 1_000_000) * cache_5m_rate
        + (cache_1h / 1_000_000) * cache_1h_rate
        + web_searches * WEB_SEARCH_COST
    )
    return cost


def compute_no_cache_cost(model_id, usage):
    """What would this cost if all tokens were charged at base input rate?"""
    pricing = get_pricing(model_id)
    if not pricing:
        return 0.0
    input_rate, output_rate = pricing[0], pricing[1]
    total_input = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    return (total_input / 1_000_000) * input_rate + (usage.get("output_tokens", 0) / 1_000_000) * output_rate


def _compute_cost_from_req(req):
    """Compute cost from a lean request dict (no raw usage dict)."""
    pricing = get_pricing(req.get("model", ""))
    if not pricing:
        return 0.0
    input_rate, output_rate, cache_read_rate, cache_5m_rate, cache_1h_rate = pricing

    inp = req.get("input_tokens", 0)
    out = req.get("output_tokens", 0)
    cr = req.get("cache_read_input_tokens", 0)
    cache_5m = req.get("cache_5m_tokens", 0)
    cache_1h = req.get("cache_1h_tokens", 0)
    cw_total = req.get("cache_creation_input_tokens", 0)
    ws = req.get("web_searches", 0)

    if cache_5m == 0 and cache_1h == 0 and cw_total > 0:
        cache_5m = cw_total

    return (
        (inp / 1_000_000) * input_rate
        + (out / 1_000_000) * output_rate
        + (cr / 1_000_000) * cache_read_rate
        + (cache_5m / 1_000_000) * cache_5m_rate
        + (cache_1h / 1_000_000) * cache_1h_rate
        + ws * WEB_SEARCH_COST
    )


def _compute_no_cache_cost_from_req(req):
    """What would this cost without caching?"""
    pricing = get_pricing(req.get("model", ""))
    if not pricing:
        return 0.0
    input_rate, output_rate = pricing[0], pricing[1]
    total_input = req.get("input_tokens", 0) + req.get("cache_read_input_tokens", 0) + req.get("cache_creation_input_tokens", 0)
    return (total_input / 1_000_000) * input_rate + (req.get("output_tokens", 0) / 1_000_000) * output_rate


def model_display_name(model_id):
    """Convert model ID to a human-friendly display name."""
    if not model_id:
        return "Unknown"
    clean = re.sub(r"-\d{8}$", "", model_id)
    parts = clean.split("-")
    # e.g. claude-opus-4-5 -> Claude Opus 4.5
    if len(parts) >= 3:
        family = parts[1].capitalize()
        version = ".".join(parts[2:])
        return f"Claude {family} {version}"
    return model_id


# ── Data Collector ─────────────────────────────────────────────────────────────

class DataCollector:
    def __init__(self):
        self._cache = None
        self._cache_time = 0
        self._lock = threading.Lock()
        self._file_mtimes = {}
        self._parsed_files = {}
        self._session_index_cache = {}

    def get_data(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._cache and (now - self._cache_time) < CACHE_TTL_SECONDS:
                return self._cache
            data = self._refresh()
            self._cache = data
            self._cache_time = now
            return data

    def get_session_detail(self, session_id):
        """Get per-request detail for a single session."""
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            jsonl_path = project_dir / f"{session_id}.jsonl"
            if jsonl_path.exists():
                return self._parse_session_detail(jsonl_path)
        return None

    def _refresh(self):
        all_projects = []
        all_sessions_meta = {}

        if not PROJECTS_DIR.exists():
            return self._empty_data()

        # Discover projects and load session indexes
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            project_name = project_dir.name
            index_path = project_dir / "sessions-index.json"
            sessions_meta = {}
            original_path = ""
            if index_path.exists():
                try:
                    with open(index_path, "r", encoding="utf-8") as f:
                        idx = json.load(f)
                    original_path = idx.get("originalPath", "")
                    for entry in idx.get("entries", []):
                        sid = entry.get("sessionId", "")
                        if sid:
                            sessions_meta[sid] = entry
                except Exception:
                    pass

            # Scan for JSONL files
            jsonl_files = list(project_dir.glob("*.jsonl"))
            all_projects.append({
                "dirName": project_name,
                "originalPath": original_path,
                "displayName": project_name.split("-")[-1] if "-" in project_name else project_name,
                "jsonlFiles": jsonl_files,
                "sessionsMeta": sessions_meta,
            })
            all_sessions_meta.update(sessions_meta)

        # Parse JSONL files (incremental)
        for proj in all_projects:
            for fp in proj["jsonlFiles"]:
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                if fp not in self._file_mtimes or self._file_mtimes[fp] != mtime:
                    self._parsed_files[fp] = self._parse_session_file(fp)
                    self._file_mtimes[fp] = mtime

        # Clean up deleted files
        existing = set()
        for proj in all_projects:
            existing.update(proj["jsonlFiles"])
        for fp in list(self._file_mtimes.keys()):
            if fp not in existing:
                del self._file_mtimes[fp]
                self._parsed_files.pop(fp, None)

        # Aggregate
        return self._aggregate(all_projects, all_sessions_meta)

    def _parse_session_file(self, filepath):
        """Parse a session JSONL, returning per-request usage data."""
        requests = []
        seen_request_ids = set()
        first_user_prompt = ""
        git_branch = ""
        session_id = ""

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    entry_type = entry.get("type", "")

                    # Extract session metadata from first entries
                    if not session_id:
                        session_id = entry.get("sessionId", "")
                    if not git_branch:
                        git_branch = entry.get("gitBranch", "")

                    # Get first user prompt
                    if entry_type == "user" and not first_user_prompt:
                        msg = entry.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    if text and not text.startswith("<ide_"):
                                        first_user_prompt = text[:200]
                                        break

                    if entry_type != "assistant":
                        continue

                    req_id = entry.get("requestId", "")
                    if not req_id or req_id in seen_request_ids:
                        continue
                    seen_request_ids.add(req_id)

                    msg = entry.get("message", {})
                    model = msg.get("model", "")
                    if model == "<synthetic>":
                        continue

                    usage = msg.get("usage", {})
                    if not usage:
                        continue

                    timestamp = entry.get("timestamp", "")

                    # Extract only the numbers we need (not the whole usage dict)
                    cache_creation = usage.get("cache_creation", {})
                    cache_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                    cache_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                    cw_total = usage.get("cache_creation_input_tokens", 0)
                    ws = 0
                    stu = usage.get("server_tool_use")
                    if isinstance(stu, dict):
                        ws = stu.get("web_search_requests", 0)

                    requests.append({
                        "requestId": req_id,
                        "model": model,
                        "timestamp": timestamp,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_input_tokens": cw_total,
                        "cache_5m_tokens": cache_5m,
                        "cache_1h_tokens": cache_1h,
                        "web_searches": ws,
                    })
        except Exception:
            pass

        return {
            "sessionId": session_id,
            "requests": requests,
            "firstUserPrompt": first_user_prompt,
            "gitBranch": git_branch,
        }

    def _parse_session_detail(self, filepath):
        """Parse a session file for detailed per-request view."""
        parsed = self._parse_session_file(filepath)
        detail_requests = []
        for req in parsed["requests"]:
            cost = _compute_cost_from_req(req)
            detail_requests.append({
                "requestId": req["requestId"],
                "model": req["model"],
                "modelDisplay": model_display_name(req["model"]),
                "timestamp": req["timestamp"],
                "inputTokens": req["input_tokens"],
                "outputTokens": req["output_tokens"],
                "cacheReadTokens": req["cache_read_input_tokens"],
                "cacheWriteTokens": req["cache_creation_input_tokens"],
                "costUSD": round(cost, 4),
            })
        return {
            "sessionId": parsed["sessionId"],
            "requests": detail_requests,
        }

    def _aggregate(self, all_projects, all_sessions_meta):
        # Accumulators
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_cost = 0.0
        total_no_cache_cost = 0.0
        total_requests = 0
        total_sessions = 0
        total_web_searches = 0

        daily = defaultdict(lambda: {
            "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
            "cacheWriteTokens": 0, "costUSD": 0.0, "requestCount": 0,
        })

        models = defaultdict(lambda: {
            "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
            "cacheWriteTokens": 0, "costUSD": 0.0, "noCacheCostUSD": 0.0,
            "requestCount": 0, "webSearches": 0,
        })

        hourly_counts = defaultdict(int)
        hourly_tokens = defaultdict(int)
        dow_counts = defaultdict(int)

        sessions_list = []
        project_agg = defaultdict(lambda: {
            "sessionCount": 0, "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0, "costUSD": 0.0,
            "requestCount": 0, "firstDate": None, "lastDate": None,
        })

        first_date = None
        last_date = None

        for proj in all_projects:
            proj_name = proj["dirName"]
            proj_display = proj["displayName"]
            proj_original = proj["originalPath"]

            for fp in proj["jsonlFiles"]:
                parsed = self._parsed_files.get(fp)
                if not parsed or not parsed["requests"]:
                    continue

                total_sessions += 1
                sid = parsed["sessionId"] or fp.stem
                meta = all_sessions_meta.get(sid, {})

                sess_input = 0
                sess_output = 0
                sess_cache_read = 0
                sess_cache_write = 0
                sess_cost = 0.0
                sess_first_ts = None
                sess_last_ts = None

                for req in parsed["requests"]:
                    model = req["model"]
                    cost = _compute_cost_from_req(req)
                    no_cache_cost = _compute_no_cache_cost_from_req(req)
                    inp = req["input_tokens"]
                    out = req["output_tokens"]
                    cr = req["cache_read_input_tokens"]
                    cw = req["cache_creation_input_tokens"]
                    ws = req.get("web_searches", 0)

                    total_input += inp
                    total_output += out
                    total_cache_read += cr
                    total_cache_write += cw
                    total_cost += cost
                    total_no_cache_cost += no_cache_cost
                    total_requests += 1
                    total_web_searches += ws

                    sess_input += inp
                    sess_output += out
                    sess_cache_read += cr
                    sess_cache_write += cw
                    sess_cost += cost

                    # Daily
                    ts = req["timestamp"]
                    date_str = ts[:10] if ts else ""
                    if date_str:
                        d = daily[date_str]
                        d["inputTokens"] += inp
                        d["outputTokens"] += out
                        d["cacheReadTokens"] += cr
                        d["cacheWriteTokens"] += cw
                        d["costUSD"] += cost
                        d["requestCount"] += 1

                        # Track date range
                        if not first_date or date_str < first_date:
                            first_date = date_str
                        if not last_date or date_str > last_date:
                            last_date = date_str

                        # Hourly
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            local_dt = dt.astimezone()
                            hour = local_dt.hour
                            hourly_counts[hour] += 1
                            hourly_tokens[hour] += inp + out + cr + cw
                            dow_counts[local_dt.weekday()] += 1
                        except Exception:
                            pass

                    # Model
                    m = models[model]
                    m["inputTokens"] += inp
                    m["outputTokens"] += out
                    m["cacheReadTokens"] += cr
                    m["cacheWriteTokens"] += cw
                    m["costUSD"] += cost
                    m["noCacheCostUSD"] += no_cache_cost
                    m["requestCount"] += 1
                    m["webSearches"] += ws

                    if not sess_first_ts or ts < sess_first_ts:
                        sess_first_ts = ts
                    if not sess_last_ts or ts > sess_last_ts:
                        sess_last_ts = ts

                # Project aggregation
                pa = project_agg[proj_name]
                pa["sessionCount"] += 1
                pa["inputTokens"] += sess_input
                pa["outputTokens"] += sess_output
                pa["cacheReadTokens"] += sess_cache_read
                pa["cacheWriteTokens"] += sess_cache_write
                pa["costUSD"] += sess_cost
                pa["requestCount"] += len(parsed["requests"])
                sess_date = (sess_first_ts or "")[:10]
                if sess_date:
                    if not pa["firstDate"] or sess_date < pa["firstDate"]:
                        pa["firstDate"] = sess_date
                    if not pa["lastDate"] or sess_date > pa["lastDate"]:
                        pa["lastDate"] = sess_date

                # Session entry
                first_prompt = meta.get("firstPrompt", "") or parsed.get("firstUserPrompt", "")
                sessions_list.append({
                    "sessionId": sid,
                    "projectPath": proj_original,
                    "projectName": proj_display,
                    "projectDirName": proj_name,
                    "firstPrompt": first_prompt[:200] if first_prompt else "",
                    "created": meta.get("created", sess_first_ts or ""),
                    "modified": meta.get("modified", sess_last_ts or ""),
                    "gitBranch": meta.get("gitBranch", parsed.get("gitBranch", "")),
                    "messageCount": meta.get("messageCount", 0),
                    "requestCount": len(parsed["requests"]),
                    "inputTokens": sess_input,
                    "outputTokens": sess_output,
                    "cacheReadTokens": sess_cache_read,
                    "cacheWriteTokens": sess_cache_write,
                    "costUSD": round(sess_cost, 4),
                })

        # Sort sessions by created date descending
        sessions_list.sort(key=lambda s: s.get("created", ""), reverse=True)

        # Build daily sorted list
        daily_list = []
        for date_str in sorted(daily.keys()):
            d = daily[date_str]
            daily_list.append({
                "date": date_str,
                "inputTokens": d["inputTokens"],
                "outputTokens": d["outputTokens"],
                "cacheReadTokens": d["cacheReadTokens"],
                "cacheWriteTokens": d["cacheWriteTokens"],
                "costUSD": round(d["costUSD"], 4),
                "requestCount": d["requestCount"],
            })

        # Build models dict
        models_out = {}
        for model_id, m in models.items():
            models_out[model_id] = {
                "displayName": model_display_name(model_id),
                "inputTokens": m["inputTokens"],
                "outputTokens": m["outputTokens"],
                "cacheReadTokens": m["cacheReadTokens"],
                "cacheWriteTokens": m["cacheWriteTokens"],
                "costUSD": round(m["costUSD"], 4),
                "noCacheCostUSD": round(m["noCacheCostUSD"], 4),
                "requestCount": m["requestCount"],
                "webSearches": m["webSearches"],
            }

        # Cache efficiency
        total_potential_input = total_input + total_cache_read + total_cache_write
        cache_hit_rate = (total_cache_read / total_potential_input * 100) if total_potential_input > 0 else 0

        # Build projects list
        projects_list = []
        for pname, pa in project_agg.items():
            # Find original path
            orig = ""
            disp = pname
            for proj in all_projects:
                if proj["dirName"] == pname:
                    orig = proj["originalPath"]
                    disp = proj["displayName"]
                    break
            projects_list.append({
                "projectDirName": pname,
                "originalPath": orig,
                "displayName": disp,
                "sessionCount": pa["sessionCount"],
                "inputTokens": pa["inputTokens"],
                "outputTokens": pa["outputTokens"],
                "cacheReadTokens": pa["cacheReadTokens"],
                "cacheWriteTokens": pa["cacheWriteTokens"],
                "costUSD": round(pa["costUSD"], 4),
                "requestCount": pa["requestCount"],
                "firstDate": pa["firstDate"],
                "lastDate": pa["lastDate"],
            })
        projects_list.sort(key=lambda p: p["costUSD"], reverse=True)

        # Hourly
        hourly_out = {str(h): hourly_counts.get(h, 0) for h in range(24)}
        hourly_tokens_out = {str(h): hourly_tokens.get(h, 0) for h in range(24)}
        dow_out = {str(d): dow_counts.get(d, 0) for d in range(7)}

        return {
            "overview": {
                "totalInputTokens": total_input,
                "totalOutputTokens": total_output,
                "totalCacheReadTokens": total_cache_read,
                "totalCacheWriteTokens": total_cache_write,
                "totalTokens": total_input + total_output + total_cache_read + total_cache_write,
                "estimatedCostUSD": round(total_cost, 2),
                "costWithoutCache": round(total_no_cache_cost, 2),
                "savingsUSD": round(total_no_cache_cost - total_cost, 2),
                "totalSessions": total_sessions,
                "totalRequests": total_requests,
                "totalWebSearches": total_web_searches,
                "firstDate": first_date,
                "lastDate": last_date,
                "projectCount": len(all_projects),
                "cacheHitRate": round(cache_hit_rate, 1),
            },
            "daily": daily_list,
            "models": models_out,
            "cache": {
                "totalCacheReadTokens": total_cache_read,
                "totalCacheWriteTokens": total_cache_write,
                "totalBaseInputTokens": total_input,
                "cacheHitRate": round(cache_hit_rate, 1),
                "costWithCache": round(total_cost, 2),
                "costWithoutCache": round(total_no_cache_cost, 2),
                "savingsUSD": round(total_no_cache_cost - total_cost, 2),
                "savingsPercent": round((total_no_cache_cost - total_cost) / total_no_cache_cost * 100, 1) if total_no_cache_cost > 0 else 0,
            },
            "sessions": sessions_list,
            "projects": projects_list,
            "hourly": {
                "hourCounts": hourly_out,
                "hourTokens": hourly_tokens_out,
                "dayOfWeekCounts": dow_out,
            },
        }

    def _empty_data(self):
        return {
            "overview": {
                "totalInputTokens": 0, "totalOutputTokens": 0,
                "totalCacheReadTokens": 0, "totalCacheWriteTokens": 0,
                "totalTokens": 0, "estimatedCostUSD": 0, "costWithoutCache": 0,
                "savingsUSD": 0, "totalSessions": 0, "totalRequests": 0,
                "totalWebSearches": 0, "firstDate": None, "lastDate": None,
                "projectCount": 0, "cacheHitRate": 0,
            },
            "daily": [],
            "models": {},
            "cache": {
                "totalCacheReadTokens": 0, "totalCacheWriteTokens": 0,
                "totalBaseInputTokens": 0, "cacheHitRate": 0,
                "costWithCache": 0, "costWithoutCache": 0,
                "savingsUSD": 0, "savingsPercent": 0,
            },
            "sessions": [],
            "projects": [],
            "hourly": {
                "hourCounts": {str(h): 0 for h in range(24)},
                "hourTokens": {str(h): 0 for h in range(24)},
                "dayOfWeekCounts": {str(d): 0 for d in range(7)},
            },
        }


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class DashboardHandler(SimpleHTTPRequestHandler):
    collector = None

    def log_message(self, format, *args):
        pass  # Suppress request logging

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "index.html not found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._html_response(SCRIPT_DIR / "index.html")
        elif path == "/api/overview":
            data = self.collector.get_data()
            self._json_response(data["overview"])
        elif path == "/api/daily":
            data = self.collector.get_data()
            self._json_response(data["daily"])
        elif path == "/api/models":
            data = self.collector.get_data()
            self._json_response(data["models"])
        elif path == "/api/cache":
            data = self.collector.get_data()
            self._json_response(data["cache"])
        elif path == "/api/sessions":
            data = self.collector.get_data()
            sessions = data["sessions"]
            project_filter = params.get("project", [None])[0]
            if project_filter:
                sessions = [s for s in sessions if s["projectDirName"] == project_filter]
            self._json_response(sessions)
        elif path.startswith("/api/session/"):
            session_id = path.split("/api/session/")[1]
            detail = self.collector.get_session_detail(session_id)
            if detail:
                self._json_response(detail)
            else:
                self._json_response({"error": "Session not found"}, 404)
        elif path == "/api/projects":
            data = self.collector.get_data()
            self._json_response(data["projects"])
        elif path == "/api/hourly":
            data = self.collector.get_data()
            self._json_response(data["hourly"])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            self.collector.get_data(force=True)
            self._json_response({"status": "ok"})
        else:
            self.send_error(404)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    collector = DataCollector()
    print("Parsing Claude Code session data...")
    collector.get_data()
    print("Data loaded.")

    DashboardHandler.collector = collector
    server = HTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"\nClaude Token Tracker running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")

    # Open browser after a short delay
    def open_browser():
        time.sleep(0.5)
        webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
