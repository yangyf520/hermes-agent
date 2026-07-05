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

Core rules: **DATABASE_QUERY_GUIDANCE**. Table and column detail lives in `~/wiki`.

## Config

- DB credentials in `~/.hermes/.env` (`DB_DSN` or `DB_TYPE` + host/user/password)
- 问数 entry: `platform_toolsets.feishu` includes `database` (prefetch auto-off)
- FAQ entry: `hermes-feishu` only, `knowledge.prefetch: true` (separate group/profile)

## Workflow

1. `read_file` → dictionary `index.md`
2. `read_file` → entity pages linked from the index
3. `schema_sample` → columns on the chosen entity page
4. `count_rows` or `database_query`

If the index and entity pages still do not resolve the table → `clarify`.
