---
name: ask-data
description: Org-specific data dictionary hints for SQL Q&A.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [sql, analytics, database]
    category: data-science
    requires_toolsets: [database]
---

# Ask-Data Skill

Core rules: **`DATABASE_QUERY_GUIDANCE`**. This bundled copy is a template only.

## Setup (your org, `~/.hermes/skills/data-science/ask-data/`)

1. Copy here; set `metadata.hermes.autoload: true` if you want org hints every data session
2. Put **your** disambiguation shortcuts in `metadata.hermes.autoload_prompt` (compact)
3. Table/column/codebook detail stays in the wiki (`llm-wiki`) — not in core, not duplicated here

## Workflow

1. `read_file` → dictionary `index.md`
2. `search_files` / `read_file` → entity pages the index points to
3. `schema_sample` → columns named on the chosen entity page
4. `count_rows` or `database_query`

If index + entities do not resolve which table → `clarify`.
