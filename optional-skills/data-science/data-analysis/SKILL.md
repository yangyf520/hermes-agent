---
name: data-analysis
description: Analyze Excel/CSV with DuckDB SQL (inspect, query).
version: 1.0.0
author: DeerFlow (Bytedance), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [data-science, analytics, excel, csv, sql, duckdb]
    category: data-science
    related_skills: [jupyter-live-kernel, chart-visualization, consulting-analysis]
---

# Data Analysis Skill

Analyze **Excel (.xlsx/.xls)** or **CSV** with DuckDB — inspect schema, run SQL,
statistical summaries, export. Ported from
[DeerFlow `data-analysis`](https://github.com/bytedance/deer-flow/tree/main/skills/public/data-analysis).

Does **not** connect to MySQL/PostgreSQL; use MCP Toolbox or `terminal` for that.

## When to Use

- User provides Excel/CSV and wants stats, pivots, filters, joins, exports
- Upstream step before `consulting-analysis` or `chart-visualization`

## Prerequisites

- Install: `hermes skills install official/data-science/data-analysis`
- Python 3.10+; script installs `duckdb` + `openpyxl` on first run if missing
- Excel requires DuckDB **spatial** extension (network on first Excel load)
- CSV-only runs do **not** need spatial

## How to Run

Script (after install, under `~/.hermes/skills/data-science/data-analysis/`):

```bash
python scripts/analyze.py --files /path/to/data.csv --action inspect
```

Run from the skill directory, or pass the full path to `scripts/analyze.py`.
Cache: `{HERMES_HOME}/.data-analysis-cache/`.

> Do NOT read the Python source; call it with the parameters below.

## Quick Reference

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--files` | Yes | One or more Excel/CSV paths |
| `--action` | Yes | `inspect`, `query`, or `summary` |
| `--sql` | For `query` | SQL against loaded tables |
| `--table` | For `summary` | Sheet or CSV base name |
| `--output-file` | No | `.csv`, `.json`, or `.md` export |

## Procedure

1. Confirm file path(s) (user path or `terminal.cwd`).
2. `--action inspect` before writing SQL.
3. Agent writes SQL from schema; `--action query`.
4. Optional `--action summary`; export large results with `--output-file`.

## Table Naming

- **Excel**: each sheet → table (`Sheet1`, `Sales`, …)
- **CSV**: filename without extension (`sales.csv` → `sales`)
- **Multi-file**: cross-file joins in one SQL context
- Sanitized names: quote with `"..."` when needed

## Pitfalls

- No NL2SQL — SQL is written by the agent from inspect output.
- Excel + restricted network may fail on spatial download.
- Prefer export for large result sets (save tokens in chat).

## Verification

After `inspect`, confirm row counts and types. Debug bad table names with
`SELECT * FROM "<table>" LIMIT 5`.
