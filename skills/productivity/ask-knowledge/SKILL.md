---
name: ask-knowledge
description: Query enterprise knowledge base for support answers.
version: 1.3.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [customer-support, knowledge-base, feishu, enterprise]
    category: productivity
---

# Ask-Knowledge Skill

Gateway prefetch runs only on FAQ-only Feishu toolsets (`hermes-feishu` without `database`).
When `platform_toolsets.feishu` also includes `database`, prefetch is off and the agent
routes 问数 via `ask-data`; FAQ uses this skill in the agent loop. Split bots/groups or
profiles for FAQ prefetch + 问数 database if you need both fast paths.

```bash
python ~/.hermes/skills/productivity/ask-knowledge/scripts/ask_knowledge.py \
  --question "<user question>" --open-id <feishu_open_id>
```

Never guess OAuth status from skill text. Run the script; if it returns an answer, use that.
If the user pastes `http://127.0.0.1:8765/callback?code=...`, wait for gateway to save the token,
then rerun the script — do not claim authorization is missing when the script succeeds.

## Prerequisites

`~/.hermes/.env` needs app credentials only: `KNOWLEDGE_APP_ID`, `KNOWLEDGE_SKILL_ID`,
`KNOWLEDGE_CLIENT_APP_ID`, `KNOWLEDGE_CLIENT_APP_SECRET`, `FEISHU_APP_ID`, `FEISHU_APP_SECRET`.

Do not hardcode `user_access_token` in `.env`. The bot exchanges per-user tokens via Feishu OAuth using the session user's `open_id` or `user_id`.

Feishu open platform → knowledge OAuth app (`KNOWLEDGE_CLIENT_APP_ID`) → Security → redirect URL must include:

`http://127.0.0.1:8765/callback`

`127.0.0.1` and `localhost` are different hostnames; complete authorization in a desktop browser, not on a phone via the chat link.

```bash
# First-time auth (local machine; opens browser and listens on port 8765):
python ask_knowledge.py --authorize --open-id ou_xxx

# Print auth URL only (no callback listener):
python ask_knowledge.py --authorize --print-url --open-id ou_xxx
```

Optional `config.yaml`:

```yaml
knowledge:
  prefetch: true
  # prefetch_with_database: true  # advanced: KB prefetch even when database toolset is on
```
