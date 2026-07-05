---
name: chart-visualization
description: Generate 26 chart types via AntV (Node.js script).
version: 1.0.0
author: AntV, DeerFlow, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [data-science, visualization, charts, antv]
    category: data-science
    related_skills: [data-analysis, consulting-analysis]
---

# Chart Visualization Skill

Transform data into chart images via **26 chart types**. Upstream:
[antvis/chart-visualization-skills](https://github.com/antvis/chart-visualization-skills)
(shipped in DeerFlow `chart-visualization`).

## When to Use

- User wants charts from structured data (trends, comparisons, maps, funnels, etc.)
- Phase 2 of `consulting-analysis` needs chart file paths

## Prerequisites

- Install: `hermes skills install official/data-science/chart-visualization`
- **Node.js ≥ 18** (`node --version`)
- **Network** to AntV API (default `https://antv-studio.alipay.com/api/gpt-vis`)
- Optional: `VIS_REQUEST_SERVER`, `SERVICE_ID` env vars (see `scripts/generate.js`)

## How to Run

From the installed skill directory:

```bash
node scripts/generate.js '<payload_json>'
```

Or full path: `~/.hermes/skills/data-science/chart-visualization/scripts/generate.js`

**Payload:**

```json
{
  "tool": "generate_line_chart",
  "args": {
    "data": [{"time": "2020", "value": 100}],
    "title": "Trend"
  }
}
```

## Workflow

1. **Select chart type** — use guidelines below; read `references/generate_<type>.md`
2. **Extract args** — map user data to the schema in the reference file
3. **Generate** — `node scripts/generate.js '<json>'`
4. **Return** — image URL from stdout + the `args` used

### Chart selection (summary)

- Time series: `generate_line_chart`, `generate_area_chart`, `generate_dual_axes_chart`
- Comparisons: `generate_bar_chart`, `generate_column_chart`, `generate_histogram_chart`
- Part-to-whole: `generate_pie_chart`, `generate_treemap_chart`
- Relationships: `generate_scatter_chart`, `generate_sankey_chart`, `generate_venn_chart`
- Maps: `generate_district_map`, `generate_pin_map`, `generate_path_map`
- See `references/` for all 26 types

## Pitfalls

- Requires external AntV service — not offline matplotlib.
- Map charts may need `SERVICE_ID`.
- Read the matching `references/*.md` before calling — schemas vary by chart type.

## Verification

Script prints a URL on success; on failure stderr shows HTTP or parse errors.

## License

`SKILL.md` and references from AntV (MIT). `scripts/generate.js` from DeerFlow port.
