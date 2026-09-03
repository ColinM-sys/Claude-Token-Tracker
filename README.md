# Claude Token Tracker

A local web dashboard for tracking your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) token usage, costs, and cache efficiency. Zero dependencies — runs entirely on Python's standard library.

![Claude Token Tracker Dashboard](screenshot.png)

*Dashboard screenshot updated September 2, 2026 — now showing accurate token tracking with recursive subagent scanning and resumed session deduplication.*

## Latest Updates (September 2026)

### Bug Fixes (Sept 2, 2026)
- **Fixed recursive file scanning**: Changed from `glob("*.jsonl")` to `rglob("*.jsonl")` to catch nested subagent transcripts in `<session-id>/subagents/workflows/` directories (was missing 82% of actual token usage)
- **Fixed resumed session double-counting**: Implemented global requestId deduplication — when sessions are resumed with `--resume`, prior transcripts copy into new files with duplicate requestIds. Now takes max(input, output, cache_read, cache_write) across all copies instead of per-file deduplication
- **Added Opus 5, Sonnet 5, Fable 5 pricing**: Updated MODEL_PRICING for latest Claude models (Opus 5: $5/$25 input/output, Sonnet 5: $2/$10, Fable 5: $10/$50)

**Impact**: Token counter now accurately tracks 100% of usage (was only showing ~83% before due to missing subagent transcripts and resumed session bugs).

## Features

- **KPI Cards** — Total tokens, estimated API cost, cache savings, and session count at a glance
- **Time Range Filtering** — View stats for Today, This Week, This Month, or All Time
- **Daily Usage Chart** — Stacked bar chart of input, output, cache write, and cache read tokens (toggle to cost view)
- **Model Breakdown** — Donut chart and table showing usage and cost per model (Opus, Sonnet, Haiku)
- **Cache Efficiency** — See how much caching saves you with hit rate and cost comparison
- **Activity by Hour** — Heatmap-style chart of when you use Claude most
- **Project Breakdown** — Token usage and cost grouped by project directory
- **Session Drill-Down** — Click any session to see per-request token and cost details
- **Auto-Refresh** — Optional 30-second auto-refresh to monitor usage in real time

## How It Works

Claude Code stores session data as JSONL files in `~/.claude/projects/`. The tracker:

1. **Recursively scans** all session files in `~/.claude/projects/` (including subagent transcripts in `<session-id>/subagents/workflows/` directories)
2. Parses each JSONL file to extract token usage per request (input, output, cache read, cache write)
3. **Deduplicates** requestIds globally — resumed sessions create duplicate requestIds across transcript copies, tracker takes max usage to avoid double-counting
4. Computes estimated API costs using current Anthropic pricing (Opus 5, Sonnet 5, Fable 5, Haiku 4.5 tiers)
5. Serves a single-page dashboard on `localhost:8050` with interactive Chart.js visualizations

All data stays local — nothing is sent anywhere.

### Data Sources Tracked
- **Main session transcripts**: `~/.claude/projects/<project>/sessions/<session-id>.jsonl`
- **Subagent transcripts**: `~/.claude/projects/<project>/sessions/<session-id>/subagents/workflows/wf_*/agent-*.jsonl` (now included)
- **Resumed sessions**: Duplicates are merged using global requestId deduplication

## Installation

```bash
# Clone the repo
git clone https://github.com/ColinM-sys/Claude-Token-Tracker.git
cd Claude-Token-Tracker

# Run it (Python 3.8+ required, no pip install needed)
python server.py
```

The dashboard opens automatically at [http://localhost:8050](http://localhost:8050).

## Requirements

- **Python 3.8+** (uses only the standard library)
- **Claude Code** installed (the dashboard reads from `~/.claude/`)

No `pip install`, no `requirements.txt`, no Node.js — just Python.

## Pricing Reference

Cost estimates use Anthropic's published API pricing for reference. Claude Code itself is a flat monthly subscription — the costs shown are what equivalent API usage would cost.

### Current Models (2026)

| Model | Input | Output | Cache Read | Cache Write (5min) |
|-------|-------|--------|------------|-------------------|
| **Claude Opus 5** | $5.00/M | $25.00/M | $0.50/M | $6.25/M |
| Claude Opus 4.8 | $5.00/M | $25.00/M | $0.50/M | $6.25/M |
| Claude Opus 4.6/4.5 | $5.00/M | $25.00/M | $0.50/M | $6.25/M |
| Claude Opus 4.1/4 | $15.00/M | $75.00/M | $1.50/M | $18.75/M |
| **Claude Sonnet 5** | $2.00/M | $10.00/M | $0.20/M | $2.50/M |
| Claude Sonnet 4.6/4.5/4 | $3.00/M | $15.00/M | $0.30/M | $3.75/M |
| **Claude Fable 5** | $10.00/M | $50.00/M | $1.00/M | $12.50/M |
| Claude Haiku 4.5 | $1.00/M | $5.00/M | $0.10/M | $1.25/M |
| Claude Haiku 3.5 | $0.80/M | $4.00/M | $0.08/M | $1.00/M |

**Legend**: M = per 1 million tokens. Bold = latest/newest model in each tier.

## License

MIT


---

## Author

Built by **Colin McDonough** — [LinkedIn](https://www.linkedin.com/in/colinmcdonoughmarketing) · [GitHub](https://github.com/ColinM-sys)
