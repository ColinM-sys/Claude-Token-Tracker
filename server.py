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
from functools import lru_cache
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PORT = 8050
CACHE_TTL_SECONDS = 30
SCRIPT_DIR = Path(__file__).resolve().parent
# Per-file parse results, so a start or refresh only re-reads transcripts that changed.
# Holds per file: size, mtime, session id, branch, and per request its id, model, time and
# token counts. Never message text.
CACHE_FILE = SCRIPT_DIR / "token_cache.json"
CACHE_FORMAT = 1           # bump when the parse rules change: an older cache is then ignored
CACHE_SAVE_SECONDS = 300   # after startup, rewrite the cache at most this often

# ── Anthropic API Pricing (per million tokens) ────────────────────────────────
# (input, output, cache_read, cache_5m_write, cache_1h_write)

MODEL_PRICING = {
    "claude-opus-5":     (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-8":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-1":   (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":     (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-5":   (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4":   (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-fable-5":    (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-haiku-4-5":  (1.00, 5.00, 0.10, 1.25, 2.00),
    "claude-haiku-3-5":  (0.80, 4.00, 0.08, 1.00, 1.60),
    "claude-haiku-3":    (0.25, 1.25, 0.03, 0.30, 0.50),
}
WEB_SEARCH_COST = 0.01  # $0.01 per search


@lru_cache(maxsize=None)
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


# ── Transcript parsing ─────────────────────────────────────────────────────────
# One request row: [requestId, model, timestamp_ms, input, output, cache_read,
#                   cache_write_total, cache_write_5m, cache_write_1h, web_searches]
R_RID, R_MODEL, R_TS, R_IN, R_OUT, R_CR, R_CW, R_C5, R_C1, R_WS = range(10)

_RID_KEY = b'"requestId"'
_USER_KEYS = (b'"type":"user"', b'"type": "user"')


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _ts_to_ms(ts):
    """ISO timestamp -> epoch milliseconds (0 when missing or unreadable)."""
    if not ts or not isinstance(ts, str):
        return 0
    try:
        return int(round(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000))
    except (ValueError, OverflowError, OSError):
        return 0


def _ms_to_iso(ms):
    """Epoch milliseconds -> '2026-10-04T12:34:56.789Z' (the format Claude Code writes)."""
    if not ms:
        return ""
    s, rem = divmod(ms, 1000)
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % rem


def _row_to_req(r):
    return {
        "model": r[R_MODEL], "input_tokens": r[R_IN], "output_tokens": r[R_OUT],
        "cache_read_input_tokens": r[R_CR], "cache_creation_input_tokens": r[R_CW],
        "cache_5m_tokens": r[R_C5], "cache_1h_tokens": r[R_C1], "web_searches": r[R_WS],
    }


def _first_prompt_of(entry):
    """The first typed text block of a user entry (200 chars), or ''."""
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                if text and isinstance(text, str) and not text.startswith("<ide_"):
                    return text[:200]
    return ""


def parse_transcript(filepath, limit=None):
    """Read a session .jsonl (its first `limit` bytes) and return its per-request usage.

    Claude Code writes ONE API reply as several assistant entries (one per content block)
    sharing a requestId; since ~2026-09-24 the first carries a partial output_tokens and a
    later one the final count. Keep the largest value of every field per requestId and the
    first timestamp, or output (the priciest tokens) is undercounted 3-5x.

    Only lines that can matter are JSON-decoded: entries with a "requestId" key, and user
    entries until the first prompt is found. The prompt text is returned under "_prompt"
    for memory only; the disk cache keeps its byte offset ("promptAt"), never the text.
    """
    rows = []
    at = {}
    sid = ""
    branch = ""
    prompt = ""
    prompt_at = -1
    pos = 0
    with open(filepath, "rb") as f:
        for raw in f:
            start = pos
            pos += len(raw)
            if limit is not None:
                if start >= limit:
                    break
                if pos > limit:
                    raw = raw[:limit - start]
            if _RID_KEY not in raw and (prompt_at >= 0 or not (_USER_KEYS[0] in raw or _USER_KEYS[1] in raw)):
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if not sid:
                sid = entry.get("sessionId") or ""
            if not branch:
                branch = entry.get("gitBranch") or ""
            etype = entry.get("type")
            if etype == "user":
                if prompt_at < 0:
                    text = _first_prompt_of(entry)
                    if text:
                        prompt, prompt_at = text, start
                continue
            if etype != "assistant":
                continue
            rid = entry.get("requestId") or ""
            msg = entry.get("message")
            if not rid or not isinstance(msg, dict):
                continue
            model = msg.get("model") or ""
            if model == "<synthetic>":
                continue
            usage = msg.get("usage")
            if not usage or not isinstance(usage, dict):
                continue
            cc = usage.get("cache_creation")
            if not isinstance(cc, dict):
                cc = {}
            stu = usage.get("server_tool_use")
            row = [
                rid, model, _ts_to_ms(entry.get("timestamp")),
                _num(usage.get("input_tokens", 0)),
                _num(usage.get("output_tokens", 0)),
                _num(usage.get("cache_read_input_tokens", 0)),
                _num(usage.get("cache_creation_input_tokens", 0)),
                _num(cc.get("ephemeral_5m_input_tokens", 0)),
                _num(cc.get("ephemeral_1h_input_tokens", 0)),
                _num(stu.get("web_search_requests", 0)) if isinstance(stu, dict) else 0,
            ]
            i = at.get(rid)
            if i is None:
                at[rid] = len(rows)
                rows.append(row)
            else:
                prev = rows[i]
                if not prev[R_TS] and row[R_TS]:
                    prev[R_TS] = row[R_TS]
                for k in range(R_IN, R_WS + 1):
                    if row[k] > prev[k]:
                        prev[k] = row[k]
    return {"sid": sid, "branch": branch, "promptAt": prompt_at, "reqs": rows, "_prompt": prompt}


# ── Data Collector ─────────────────────────────────────────────────────────────

class DataCollector:
    def __init__(self, cache_file=CACHE_FILE):
        self._cache = None
        self._cache_time = 0
        self._lock = threading.Lock()
        self._cache_file = Path(cache_file) if cache_file else None
        self._files = {}     # path -> {"size", "mtime", "sid", "branch", "promptAt", "reqs"}
        self._prompts = {}   # path -> first prompt text (memory only, never written to disk)
        self._dirty = False
        self._last_save = 0.0
        self.last_refresh = {"files": 0, "parsed": 0, "seconds": 0.0}
        self._load_cache()

    def get_data(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._cache and (now - self._cache_time) < CACHE_TTL_SECONDS:
                return self._cache
            data = self._refresh()
            if self._dirty and (force or time.time() - self._last_save >= CACHE_SAVE_SECONDS):
                self._save_cache()
            self._cache = data
            self._cache_time = now
            return data

    def save_cache(self):
        with self._lock:
            if self._dirty:
                self._save_cache()

    def get_session_detail(self, session_id):
        """Get per-request detail for a single session."""
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            jsonl_path = project_dir / f"{session_id}.jsonl"
            if jsonl_path.exists():
                return self._parse_session_detail(jsonl_path)
        return None

    # ── Disk cache ──

    def _load_cache(self):
        if not self._cache_file or not self._cache_file.exists():
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("format") != CACHE_FORMAT:
                return
            models = doc["models"]
            files = {}
            for path, e in doc["files"].items():
                files[path] = {
                    "size": e["size"], "mtime": e["mtime"], "sid": e["sid"], "branch": e["branch"],
                    "promptAt": e["promptAt"],
                    "reqs": [[r[0], models[r[1]]] + r[2:] for r in e["reqs"]],
                }
            self._files = files
        except Exception as exc:  # unreadable or old cache: start from scratch
            print(f"Ignoring {self._cache_file.name}: {exc}")
            self._files = {}

    def _save_cache(self):
        if not self._cache_file:
            return
        models = {}
        files = {}
        for path, e in self._files.items():
            files[path] = {
                "size": e["size"], "mtime": e["mtime"], "sid": e["sid"], "branch": e["branch"],
                "promptAt": e["promptAt"],
                "reqs": [[r[R_RID], models.setdefault(r[R_MODEL], len(models))] + r[R_TS:] for r in e["reqs"]],
            }
        doc = {"format": CACHE_FORMAT, "models": list(models), "files": files}
        tmp = self._cache_file.with_name(self._cache_file.name + ".tmp")
        try:
            text = json.dumps(doc, separators=(",", ":"))  # dumps (C encoder) is ~10x faster than dump
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self._cache_file)
            self._dirty = False
            self._last_save = time.time()
        except OSError as exc:  # e.g. disk full: keep serving from memory
            print(f"Could not write {self._cache_file.name}: {exc}")
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── Refresh ──

    def _refresh(self):
        t0 = time.time()
        all_projects = []
        all_sessions_meta = {}

        if not PROJECTS_DIR.exists():
            return self._empty_data()

        # Discover projects and load session indexes
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
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

            # Every .jsonl, recursively (subagent transcripts too), in every project. Benchmark
            # projects (C--ccbench-*) are real usage and are counted like any other project.
            jsonl_files = sorted(str(p) for p in project_dir.rglob("*.jsonl"))
            all_projects.append({
                "dirName": project_name,
                "originalPath": original_path,
                "displayName": project_name.split("-")[-1] if "-" in project_name else project_name,
                "jsonlFiles": jsonl_files,
                "sessionsMeta": sessions_meta,
            })
            all_sessions_meta.update(sessions_meta)

        # Parse only new or changed files (by size and mtime); the rest come from the cache.
        seen = set()
        parsed = 0
        for proj in all_projects:
            for fp in proj["jsonlFiles"]:
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                seen.add(fp)
                e = self._files.get(fp)
                if e is not None and e["size"] == st.st_size and e["mtime"] == st.st_mtime_ns:
                    continue
                try:
                    e = parse_transcript(fp, limit=st.st_size)
                except OSError:
                    continue  # unreadable right now: keep any earlier result
                prompt = e.pop("_prompt")
                e["size"] = st.st_size
                e["mtime"] = st.st_mtime_ns
                self._files[fp] = e
                if prompt:
                    self._prompts[fp] = prompt
                else:
                    self._prompts.pop(fp, None)
                self._dirty = True
                parsed += 1

        # Forget deleted files
        for fp in list(self._files):
            if fp not in seen:
                del self._files[fp]
                self._prompts.pop(fp, None)
                self._dirty = True

        data = self._aggregate(all_projects, all_sessions_meta)
        self.last_refresh = {"files": len(seen), "parsed": parsed, "seconds": round(time.time() - t0, 2)}
        return data

    def _first_prompt(self, fp, e):
        """First prompt of a file: from memory, else re-read from its line offset."""
        if fp in self._prompts:
            return self._prompts[fp]
        text = ""
        if e["promptAt"] >= 0:
            try:
                with open(fp, "rb") as f:
                    f.seek(e["promptAt"])
                    entry = json.loads(f.readline())
                if isinstance(entry, dict):
                    text = _first_prompt_of(entry)
            except (OSError, ValueError):
                pass
        self._prompts[fp] = text
        return text

    def _parse_session_detail(self, filepath):
        """Parse a session file for detailed per-request view."""
        parsed = parse_transcript(filepath)
        detail_requests = []
        for r in parsed["reqs"]:
            cost = _compute_cost_from_req(_row_to_req(r))
            detail_requests.append({
                "requestId": r[R_RID],
                "model": r[R_MODEL],
                "modelDisplay": model_display_name(r[R_MODEL]),
                "timestamp": _ms_to_iso(r[R_TS]),
                "inputTokens": r[R_IN],
                "outputTokens": r[R_OUT],
                "cacheReadTokens": r[R_CR],
                "cacheWriteTokens": r[R_CW],
                "costUSD": round(cost, 4),
            })
        return {
            "sessionId": parsed["sid"],
            "requests": detail_requests,
        }

    def _aggregate(self, all_projects, all_sessions_meta):
        # Session files that hold at least one request, in project order
        sess = []  # (proj, path, entry, first_ts_ms, last_ts_ms)
        for proj in all_projects:
            for fp in proj["jsonlFiles"]:
                e = self._files.get(fp)
                if not e or not e["reqs"]:
                    continue
                stamps = [r[R_TS] for r in e["reqs"] if r[R_TS]]
                sess.append((proj, fp, e, min(stamps) if stamps else 0, max(stamps) if stamps else 0))

        # One row per requestId across ALL files. A resumed session's transcript repeats the
        # earlier requests: each counts once, with the largest value of every field and the
        # earliest timestamp. It is credited to the earliest file holding it (by first request
        # time, then mtime), so sessions and projects add up to the totals.
        order = sorted(range(len(sess)), key=lambda i: (sess[i][3] or float("inf"), sess[i][2]["mtime"], sess[i][1]))
        uniq = {}  # rid -> [model, ts, in, out, cr, cw, c5, c1, ws, owner]
        for i in order:
            for r in sess[i][2]["reqs"]:
                cur = uniq.get(r[R_RID])
                if cur is None:
                    cur = r[1:]
                    cur.append(i)
                    uniq[r[R_RID]] = cur
                else:
                    ts = r[R_TS]
                    if ts and (not cur[1] or ts < cur[1]):
                        cur[1] = ts
                    for k in range(2, 9):
                        if r[k + 1] > cur[k]:
                            cur[k] = r[k + 1]

        # Accumulators
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_cost = 0.0
        total_no_cache_cost = 0.0
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
        local_minute = {}  # minute since epoch -> (local date, hour, weekday)

        sess_sums = [[0, 0, 0, 0, 0.0, 0] for _ in sess]  # in, out, cache read, cache write, cost, requests

        first_date = None
        last_date = None

        for model, ts, inp, out, cr, cw, c5, c1, ws, owner in uniq.values():
            req = {"model": model, "input_tokens": inp, "output_tokens": out,
                   "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
                   "web_searches": ws}
            cost = _compute_cost_from_req(req)
            no_cache_cost = _compute_no_cache_cost_from_req(req)

            total_input += inp
            total_output += out
            total_cache_read += cr
            total_cache_write += cw
            total_cost += cost
            total_no_cache_cost += no_cache_cost
            total_web_searches += ws

            s = sess_sums[owner]
            s[0] += inp
            s[1] += out
            s[2] += cr
            s[3] += cw
            s[4] += cost
            s[5] += 1

            # Daily and hourly, by local time
            if ts:
                minute = ts // 60000
                loc = local_minute.get(minute)
                if loc is None:
                    dt = datetime.fromtimestamp(minute * 60)
                    loc = local_minute[minute] = (dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday())
                date_str, hour, weekday = loc
                d = daily[date_str]
                d["inputTokens"] += inp
                d["outputTokens"] += out
                d["cacheReadTokens"] += cr
                d["cacheWriteTokens"] += cw
                d["costUSD"] += cost
                d["requestCount"] += 1

                if not first_date or date_str < first_date:
                    first_date = date_str
                if not last_date or date_str > last_date:
                    last_date = date_str

                hourly_counts[hour] += 1
                hourly_tokens[hour] += inp + out + cr + cw
                dow_counts[weekday] += 1

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

        sessions_list = []
        project_agg = defaultdict(lambda: {
            "sessionCount": 0, "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0, "costUSD": 0.0,
            "requestCount": 0, "firstDate": None, "lastDate": None,
        })

        for i, (proj, fp, e, first_ts, last_ts) in enumerate(sess):
            s_in, s_out, s_cr, s_cw, s_cost, s_reqs = sess_sums[i]
            sid = e["sid"] or Path(fp).stem
            meta = all_sessions_meta.get(sid, {})
            sess_first_ts = _ms_to_iso(first_ts)
            sess_last_ts = _ms_to_iso(last_ts)

            # Project aggregation
            pa = project_agg[proj["dirName"]]
            pa["sessionCount"] += 1
            pa["inputTokens"] += s_in
            pa["outputTokens"] += s_out
            pa["cacheReadTokens"] += s_cr
            pa["cacheWriteTokens"] += s_cw
            pa["costUSD"] += s_cost
            pa["requestCount"] += s_reqs
            sess_date = sess_first_ts[:10]
            if sess_date:
                if not pa["firstDate"] or sess_date < pa["firstDate"]:
                    pa["firstDate"] = sess_date
                if not pa["lastDate"] or sess_date > pa["lastDate"]:
                    pa["lastDate"] = sess_date

            # Session entry
            first_prompt = meta.get("firstPrompt", "") or self._first_prompt(fp, e)
            sessions_list.append({
                "sessionId": sid,
                "projectPath": proj["originalPath"],
                "projectName": proj["displayName"],
                "projectDirName": proj["dirName"],
                "firstPrompt": first_prompt[:200] if first_prompt else "",
                "created": meta.get("created", sess_first_ts),
                "modified": meta.get("modified", sess_last_ts),
                "gitBranch": meta.get("gitBranch", e["branch"]),
                "messageCount": meta.get("messageCount", 0),
                "requestCount": s_reqs,
                "inputTokens": s_in,
                "outputTokens": s_out,
                "cacheReadTokens": s_cr,
                "cacheWriteTokens": s_cw,
                "costUSD": round(s_cost, 4),
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
        proj_by_name = {p["dirName"]: p for p in all_projects}
        for pname, pa in project_agg.items():
            proj = proj_by_name[pname]
            projects_list.append({
                "projectDirName": pname,
                "originalPath": proj["originalPath"],
                "displayName": proj["displayName"],
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
                "totalSessions": len(sess),
                "totalRequests": len(uniq),
                "totalWebSearches": total_web_searches,
                "firstDate": first_date,
                "lastDate": last_date,
                # Projects with usage (there are hundreds of empty benchmark project folders)
                "projectCount": len(projects_list),
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
    lr = collector.last_refresh
    print(f"Data loaded in {lr['seconds']}s: {lr['parsed']} of {lr['files']} transcript files parsed, "
          f"the rest from {CACHE_FILE.name}.")

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
        collector.save_cache()
        server.shutdown()


if __name__ == "__main__":
    main()
