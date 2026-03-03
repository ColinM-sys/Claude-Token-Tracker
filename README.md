# Claude Token Tracker

A local web dashboard for tracking your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) token usage, costs, and cache efficiency. Zero dependencies — runs entirely on Python's standard library.

![Claude Token Tracker Dashboard](screenshot.png)

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

1. Scans all session files in `~/.claude/projects/`
2. Parses each JSONL file to extract token usage per request (input, output, cache read, cache write)
3. Computes estimated API costs using current Anthropic pricing
4. Serves a single-page dashboard on `localhost:8050` with interactive Chart.js visualizations

All data stays local — nothing is sent anywhere.

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

| Model | Input | Output | Cache Read | Cache Write (5min) |
|-------|-------|--------|------------|-------------------|
| Claude Opus 4.5/4.6 | $5.00/M | $25.00/M | $0.50/M | $6.25/M |
| Claude Sonnet 4/4.5/4.6 | $3.00/M | $15.00/M | $0.30/M | $3.75/M |
| Claude Haiku 4.5 | $1.00/M | $5.00/M | $0.10/M | $1.25/M |

## License

MIT
