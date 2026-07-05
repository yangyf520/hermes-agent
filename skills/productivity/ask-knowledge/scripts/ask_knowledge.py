#!/usr/bin/env python3
"""Feishu enterprise knowledge base (Aily): per-user OAuth, gateway prefetch."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ANSWER_KEYS = ("answer", "content", "text", "result", "output", "message")
_REFRESH_SKEW = 300
_OAUTH_REDIRECT = "http://127.0.0.1:8765/callback"
OAUTH_OK = "Feishu authorization saved. Ask your question again."
_oauth_listener_lock = threading.Lock()
_oauth_listener_active = False


def _env(k: str, default: str = "") -> str:
    return (os.getenv(k, default) or "").strip()


def _session_user() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID", "") or ""
    except Exception:
        return _env("HERMES_SESSION_USER_ID")


def _load_dotenv() -> None:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
    except Exception:
        pass


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path(_env("HERMES_HOME", str(Path.home() / ".hermes")))


def _api_base() -> str:
    return (_env("KNOWLEDGE_BASE_URL") or "https://open.feishu.cn").rstrip("/")


def _feishu_err(payload: dict | str, *, http_code: int | None = None) -> str:
    if isinstance(payload, str):
        base = payload.strip()
        return f"Feishu HTTP {http_code}: {base}" if http_code else f"Feishu: {base}"
    parts: list[str] = []
    if (code := payload.get("code")) is not None:
        parts.append(f"code={code}")
    for key in ("error_description", "error", "msg", "message"):
        if v := payload.get(key):
            parts.append(str(v))
    detail = " ".join(parts) if parts else json.dumps(payload, ensure_ascii=False)
    return f"Feishu HTTP {http_code}: {detail}" if http_code else f"Feishu API: {detail}"


def _http(path: str, body: dict | None = None, *, bearer: str = "", method: str = "GET") -> dict:
    url = path if path.startswith("http") else f"{_api_base()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            raise RuntimeError(_feishu_err(json.loads(raw), http_code=exc.code)) from exc
        except json.JSONDecodeError:
            raise RuntimeError(_feishu_err(raw, http_code=exc.code)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Feishu request failed: {exc}") from exc
    if "data:" in raw and not raw.lstrip().startswith("{"):
        for line in reversed(raw.splitlines()):
            if line.startswith("data:") and (chunk := line[5:].strip()) and chunk != "[DONE]":
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    pass
        return {}
    payload = json.loads(raw)
    if int(payload.get("code", 0)) not in (0,):
        raise RuntimeError(_feishu_err(payload))
    return payload


def _oauth_cache_path() -> Path:
    return _home() / ".cache" / "ask-knowledge-oauth.json"


def _legacy_oauth_cache_path() -> Path:
    return _home() / "feishu_knowledge_tokens.json"


def _token_store() -> dict:
    path = _oauth_cache_path()
    legacy = _legacy_oauth_cache_path()
    if not path.is_file() and legacy.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(legacy.read_bytes())
            legacy.unlink()
        except OSError:
            path = legacy
    elif not path.is_file():
        return {}
    else:
        path = _oauth_cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tokens(store: dict) -> None:
    p = _oauth_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _oauth_post(body: dict) -> dict:
    return _http("/open-apis/authen/v2/oauth/token", body, method="POST")


def _identity_keys(sender: str, open_id: str = "") -> list[str]:
    sid, oid = (sender or "").strip(), (open_id or "").strip()
    if not oid and sid.startswith("ou_"):
        oid = sid
    keys = [k for k in (oid, sid) if k]
    if tenant := _tenant_user_id(oid or sid):
        if tenant not in keys:
            keys.append(tenant)
    return keys


def _ou_to_user_id(open_id: str) -> str:
    oid = (open_id or "").strip()
    if not oid.startswith("ou_"):
        return ""
    app_id, secret = _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET")
    try:
        tenant = _http(
            "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": secret},
            method="POST",
        ).get("tenant_access_token") or ""
        if not tenant:
            return ""
        path = f"/open-apis/contact/v3/users/{urllib.parse.quote(oid, safe='')}?user_id_type=open_id"
        user = (_http(path, bearer=tenant).get("data") or {}).get("user") or {}
        return str(user.get("user_id") or "")
    except RuntimeError as exc:
        print(f"Feishu open_id to user_id lookup failed: {exc}", file=sys.stderr)
        return ""


def _tenant_user_id(who: str = "") -> str:
    who = (who or _session_user()).strip()
    if not who or who.startswith("on_"):
        return ""
    return _ou_to_user_id(who) if who.startswith("ou_") else who


_biz_user_id = _tenant_user_id


def _oauth_token_fields(payload: dict) -> dict:
    """Feishu v2 token responses nest tokens under ``data``."""
    data = payload.get("data")
    if isinstance(data, dict) and (
        data.get("access_token") or data.get("refresh_token") or data.get("expires_in")
    ):
        return data
    return payload


def _store_token(who: str, payload: dict, *, open_id: str = "") -> str:
    fields = _oauth_token_fields(payload)
    entry = {
        "access_token": str(fields.get("access_token") or ""),
        "refresh_token": str(fields.get("refresh_token") or ""),
        "expires_at": int(time.time()) + int(fields.get("expires_in") or 0),
    }
    store = _token_store()
    users = store.setdefault("users", {})
    for key in _identity_keys(who, open_id):
        users[key] = dict(entry)
    _save_tokens(store)
    return entry["access_token"]


def _user_access_token_detail(sender: str, *, open_id: str = "") -> tuple[str, str]:
    """Return (access_token, feishu_error). feishu_error set when refresh/API failed."""
    users = _token_store().get("users") or {}
    if not isinstance(users, dict):
        return "", ""
    now = int(time.time())
    cid, secret = _env("KNOWLEDGE_CLIENT_APP_ID"), _env("KNOWLEDGE_CLIENT_APP_SECRET")
    had_entry = False
    last_err = ""
    for key in _identity_keys(sender, open_id):
        entry = users.get(key) or {}
        if not isinstance(entry, dict) or not entry:
            continue
        had_entry = True
        access = str(entry.get("access_token") or "")
        refresh = str(entry.get("refresh_token") or "")
        exp = int(entry.get("expires_at") or 0)
        if access and exp > now + _REFRESH_SKEW:
            return access, ""
        if refresh and cid and secret:
            try:
                return _store_token(key, _oauth_post({
                    "grant_type": "refresh_token",
                    "client_id": cid,
                    "client_secret": secret,
                    "refresh_token": refresh,
                })), ""
            except RuntimeError as exc:
                last_err = str(exc)
    if had_entry and last_err:
        return "", last_err
    return "", ""


def _user_access_token(sender: str, *, open_id: str = "") -> str:
    tok, _ = _user_access_token_detail(sender, open_id=open_id)
    return tok


fetch_user_access_token_via_feishu_api = _user_access_token


def _oauth_redirect_uri() -> str:
    raw = _env("KNOWLEDGE_OAUTH_REDIRECT_URI", _OAUTH_REDIRECT)
    # Feishu whitelist is exact-match; normalise localhost to 127.0.0.1.
    if raw.startswith("http://localhost:"):
        raw = "http://127.0.0.1:" + raw.split("://localhost:", 1)[1]
    elif raw.startswith("https://localhost:"):
        raw = "https://127.0.0.1:" + raw.split("://localhost:", 1)[1]
    return raw


def _oauth_redirect_parts() -> tuple[str, str, int, str]:
    redirect = _oauth_redirect_uri()
    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    path = parsed.path or "/callback"
    return redirect, host, port, path


def _authorize_url(state: str = "") -> str:
    app_id = _env("KNOWLEDGE_CLIENT_APP_ID")
    if not app_id:
        raise RuntimeError("Missing KNOWLEDGE_CLIENT_APP_ID")
    params = {
        "client_id": app_id,
        "response_type": "code",
        "redirect_uri": _oauth_redirect_uri(),
        **({"state": state} if state else {}),
    }
    scope = _env("KNOWLEDGE_OAUTH_SCOPE", "aily:skill:write")
    if scope:
        params["scope"] = scope
    q = urllib.parse.urlencode(params)
    base = _env("KNOWLEDGE_OAUTH_AUTHORIZE_BASE", "https://accounts.feishu.cn").rstrip("/")
    return f"{base}/open-apis/authen/v1/authorize?{q}"


def _oauth_callback_handler(path: str, code_box: list[str], error_box: list[str]) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            req_path = urllib.parse.urlparse(self.path).path
            if req_path != path:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if err := (qs.get("error") or [""])[0]:
                error_box.append(err)
            elif code := (qs.get("code") or [""])[0]:
                code_box.append(code)
            body = (
                "<html><body><p>Authorization saved. Close this page and return to Feishu.</p></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    return _Handler


def _listen_for_oauth_code(user_key: str, *, open_id: str = "", timeout: float = 180.0) -> str:
    redirect, host, port, path = _oauth_redirect_parts()
    bind_host = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else host
    code_box: list[str] = []
    error_box: list[str] = []
    server = HTTPServer((bind_host, port), _oauth_callback_handler(path, code_box, error_box))
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + max(5.0, timeout)
        while time.monotonic() < deadline:
            if code_box:
                oid = (open_id or "").strip() or (user_key if user_key.startswith("ou_") else "")
                return _exchange_code(code_box[0], user_key or open_id, open_id=oid)
            if error_box:
                raise RuntimeError(f"OAuth denied: {error_box[0]}")
            time.sleep(0.1)
        raise RuntimeError(
            f"OAuth timed out after {int(timeout)}s — no callback on {redirect}. "
            "If Feishu returns 20029, add this redirect URL under Security in the knowledge app."
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _start_background_oauth_listener(user_key: str, *, open_id: str = "", timeout: float = 300.0) -> bool:
    """Listen on the OAuth redirect port so the browser callback succeeds on this machine."""
    global _oauth_listener_active
    with _oauth_listener_lock:
        if _oauth_listener_active:
            return False
        _oauth_listener_active = True

    def _worker() -> None:
        global _oauth_listener_active
        try:
            _listen_for_oauth_code(user_key, open_id=open_id, timeout=timeout)
        except Exception as exc:
            print(f"OAuth callback listener failed: {exc}", file=sys.stderr)
        finally:
            with _oauth_listener_lock:
                _oauth_listener_active = False

    threading.Thread(target=_worker, daemon=True, name="ask-knowledge-oauth").start()
    return True


def _authorize_listen(user_key: str, timeout: float = 180.0, *, open_id: str = "") -> str:
    redirect, _, _, _ = _oauth_redirect_parts()
    url = _authorize_url(open_id or user_key)
    print(f"Listening on {redirect}", file=sys.stderr)
    print(f"Open: {url}", file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return _listen_for_oauth_code(user_key, open_id=open_id, timeout=timeout)


authorize_url_for_user = lambda user_key: _authorize_url((user_key or "").strip())


def extract_oauth_code_from_text(text: str) -> str:
    raw = (text or "").strip()
    if "code=" not in raw:
        return ""
    url = raw if "://" in raw else f"http://x/?{raw.lstrip('?')}"
    try:
        return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("code") or [""])[0]
    except Exception:
        return ""


def extract_oauth_state_from_text(text: str) -> str:
    raw = (text or "").strip()
    if "state=" not in raw:
        return ""
    url = raw if "://" in raw else f"http://x/?{raw.lstrip('?')}"
    try:
        state = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state") or [""])[0]
        state = str(state or "").strip()
        return state if state.startswith("ou_") else ""
    except Exception:
        return ""


def _oauth_callback_open_id(text: str, *, feishu_open_id: str = "", biz_user_id: str = "") -> str:
    return (
        feishu_open_id
        or extract_oauth_state_from_text(text)
        or (biz_user_id if str(biz_user_id or "").startswith("ou_") else "")
    )


def _exchange_code(code: str, who: str, *, open_id: str = "") -> str:
    cid, secret = _env("KNOWLEDGE_CLIENT_APP_ID"), _env("KNOWLEDGE_CLIENT_APP_SECRET")
    if not cid or not secret:
        raise RuntimeError("Missing KNOWLEDGE_CLIENT_APP_ID/SECRET")
    payload = _oauth_post({
        "grant_type": "authorization_code",
        "client_id": cid,
        "client_secret": secret,
        "code": code.strip(),
        "redirect_uri": _oauth_redirect_uri(),
    })
    return _store_token(who, payload, open_id=open_id or (who if who.startswith("ou_") else ""))


def _feishu_open_id(raw) -> str:
    if raw is None:
        return ""
    try:
        ev = getattr(raw, "event", None) or raw
        if isinstance(ev, dict):
            oid = ((ev.get("sender") or {}).get("sender_id") or {}).get("open_id") or ""
        else:
            s = getattr(ev, "sender", None)
            sid = getattr(s, "sender_id", None) if s else None
            oid = getattr(sid, "open_id", None) if sid else None
        oid = str(oid or "").strip()
        return oid if oid.startswith("ou_") else ""
    except Exception:
        return ""


def _extract_answer(node) -> str:
    if isinstance(node, str):
        t = node.strip()
        return "" if not t or t.lower() in ("undefined", "success", "failed") else t
    if isinstance(node, dict):
        for k in _ANSWER_KEYS:
            if isinstance(v := node.get(k), str) and (t := v.strip()) and t.lower() != "undefined":
                return t
        if isinstance(raw := node.get("data"), dict) and isinstance(out := raw.get("output"), str):
            try:
                return _extract_answer(json.loads(out))
            except json.JSONDecodeError:
                return _extract_answer(out)
        for v in node.values():
            if ans := _extract_answer(v):
                return ans
    if isinstance(node, list):
        for item in node:
            if ans := _extract_answer(item):
                return ans
    return ""


def _format_kb_reply(raw: str, *, max_chars: int = 6000) -> str:
    """Turn Aily JSON recall hits into readable markdown for direct reply."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:max_chars]
    if not isinstance(data, list):
        return text[:max_chars]
    parts: list[str] = []
    for i, item in enumerate(data[:3]):
        if not isinstance(item, dict):
            continue
        src = item.get("sourceValue") or {}
        if not isinstance(src, dict):
            continue
        meta = src.get("meta") or {}
        title = str((meta.get("title") if isinstance(meta, dict) else "") or item.get("recallSourceQuery") or f"Result {i + 1}")
        content = str(src.get("content") or "").strip()
        if content:
            parts.append(f"### {title}\n{content}")
    if not parts:
        return text[:max_chars]
    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n..."
    return body


def _skill_body(question: str, biz_user: str) -> dict:
    md = json.dumps({
        "title": "",
        "content": {"widgets": [{"type": "Markdown", "props": {"content": question}}]},
    }, ensure_ascii=False)
    sid = zlib.crc32(biz_user.encode()) + (1 << 31)
    return {
        "global_variable": {"query": question},
        "input": json.dumps({
            "userInput": question,
            "userMessage": {
                "sender": {"id": biz_user, "senderType": "USER", "channelType": "LARK_OPEN_API_SKILL"},
                "session": {"channelType": "LARK_OPEN_API_SKILL", "sessionID": sid},
                "content": md,
            },
        }, ensure_ascii=False),
    }


def _build_query(app_id: str, question: str, biz_user: str) -> tuple[str, dict, dict[str, str]]:
    skill = _env("KNOWLEDGE_SKILL_ID")
    if not skill:
        raise RuntimeError("Missing KNOWLEDGE_SKILL_ID")
    tpl = _env("KNOWLEDGE_SKILL_START_PATH", "/open-apis/aily/v1/apps/{app_id}/skills/{skill_id}/start")
    path = tpl.replace("{app_id}", app_id).replace("{skill_id}", skill)
    hdrs = {"Accept": "text/event-stream, application/json", "X-aily-BizUserID": biz_user}
    return path, _skill_body(question, biz_user), hdrs


def _ask(question: str, sender: str, *, open_id: str = "") -> dict:
    _load_dotenv()
    if sender:
        os.environ["HERMES_SESSION_USER_ID"] = sender
    app_id = _env("KNOWLEDGE_APP_ID") or _env("AILY_APP_ID")
    if not app_id:
        return {"success": False, "error": "Missing KNOWLEDGE_APP_ID"}
    oid = open_id or (sender if sender.startswith("ou_") else "")
    if not (biz := _tenant_user_id(oid or sender)):
        if (oid or sender).startswith("ou_"):
            return {
                "success": False,
                "error": "Could not resolve user_id from open_id; grant contact:user.employee_id:readonly",
            }
        return {"success": False, "error": "Missing Feishu sender id"}
    tok, feishu_err = _user_access_token_detail(sender, open_id=oid)
    if not tok:
        if feishu_err:
            return {"success": False, "error": feishu_err}
        return {"success": False, "error": f"User {biz} has not completed Feishu OAuth authorization"}
    try:
        path, body, hdrs = _build_query(app_id, question.strip(), biz)
        answer = _extract_answer(_http(path, body, bearer=tok, method="POST"))
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "answer": answer, "found": bool(answer)}


_query_knowledge = _ask


def _knowledge_credentials_configured() -> bool:
    return bool(
        (_env("KNOWLEDGE_APP_ID") or _env("AILY_APP_ID"))
        and _env("KNOWLEDGE_SKILL_ID")
        and _env("KNOWLEDGE_CLIENT_APP_ID")
        and _env("KNOWLEDGE_CLIENT_APP_SECRET")
    )


def _warm_oauth_listener_if_needed(session: str, *, open_id: str = "") -> None:
    """While user token is missing, keep the redirect port open on the gateway host."""
    if not _knowledge_credentials_configured():
        return
    tok, _ = _user_access_token_detail(session, open_id=open_id)
    if tok:
        return
    _start_background_oauth_listener(session, open_id=open_id)


def _prefetch_on(config: dict | None, platform: str = "feishu") -> bool:
    if (config or {}).get("knowledge", {}).get("prefetch") is False:
        return False
    if not _knowledge_credentials_configured():
        return False
    # Dual entry: when this platform also has database tools, KB prefetch must not
    # run on every message — route 问数 via ask-data + database in the agent instead.
    # Opt in with knowledge.prefetch_with_database: true (FAQ/data share one bot).
    if (config or {}).get("knowledge", {}).get("prefetch_with_database") is not True:
        toolsets = (config or {}).get("platform_toolsets", {}).get(platform) or []
        if isinstance(toolsets, list):
            names = {str(t) for t in toolsets}
            if "database" in names or "all" in names:
                return False
    return True


prefetch_configured = lambda: _prefetch_on(None)
_should_prefetch = lambda text: bool((text or "").strip()) and not (text or "").strip().startswith("/")
_prefetch_enabled = _prefetch_on


def _oauth_chat_reply(
    biz_user_id: str,
    *,
    open_id: str = "",
    session: str = "",
    listener: bool = False,
) -> str:
    tenant = _tenant_user_id(open_id or biz_user_id) or biz_user_id
    try:
        url = _authorize_url(open_id or session or biz_user_id)
    except RuntimeError:
        url = ""
    if url:
        _, _, port, _ = _oauth_redirect_parts()
        if listener:
            return (
                f"Knowledge base access requires your authorization (user {tenant}).\n"
                f"1. Open this link in a desktop browser on the same machine as the gateway: {url}\n"
                f"2. After approving, you should see \"Authorization saved\" "
                f"(listener on 127.0.0.1:{port}).\n"
                f"3. Return to Feishu and ask your question again.\n"
                f"If the browser still shows connection refused, copy the full URL from "
                f"the address bar and paste it back into this Feishu chat."
            )
        return (
            f"Knowledge base access requires your authorization (user {tenant}).\n"
            f"1. Open in a desktop browser: {url}\n"
            f"2. Copy the full callback URL from the address bar and paste it back into this Feishu chat."
        )
    return f"User {tenant} needs Feishu OAuth; configure KNOWLEDGE_CLIENT_APP_ID."


def _oauth_token_saved(*, session: str, open_id: str = "", biz_user_id: str = "", text: str = "") -> bool:
    """True when any identity key for this user already has a valid token."""
    state_oid = extract_oauth_state_from_text(text)
    candidates: list[tuple[str, str]] = []
    for sid in (session, open_id, state_oid, biz_user_id):
        sid = str(sid or "").strip()
        if not sid:
            continue
        oid = open_id or state_oid or (sid if sid.startswith("ou_") else "")
        if (sid, oid) not in candidates:
            candidates.append((sid, oid))
    for sid, oid in candidates:
        if _user_access_token_detail(sid, open_id=oid)[0]:
            return True
    return False


def gateway_prefetch(
    message_text: str,
    biz_user_id: str,
    config: dict | None = None,
    *,
    feishu_open_id: str = "",
    feishu_raw_event=None,
) -> dict:
    noop = {"api_message": message_text, "persist_user": None, "kb_found": None, "direct_reply": None}
    if not _should_prefetch(message_text):
        return noop

    text = message_text.strip()
    oid = (
        feishu_open_id
        or _feishu_open_id(feishu_raw_event)
        or _oauth_callback_open_id(text, feishu_open_id=feishu_open_id, biz_user_id=biz_user_id)
        or (biz_user_id if biz_user_id.startswith("ou_") else "")
    )
    session = oid or biz_user_id

    if code := extract_oauth_code_from_text(text):
        oid = _oauth_callback_open_id(text, feishu_open_id=oid, biz_user_id=biz_user_id) or oid
        session = oid or biz_user_id or extract_oauth_state_from_text(text)
        if not session:
            return {
                **noop,
                "kb_found": False,
                "direct_reply": "Authorization failed: missing user id in callback URL",
            }
        try:
            _exchange_code(code, session, open_id=oid)
            return {**noop, "kb_found": False, "direct_reply": OAUTH_OK}
        except RuntimeError as exc:
            if _oauth_token_saved(session=session, open_id=oid, biz_user_id=biz_user_id, text=text):
                return {**noop, "kb_found": False, "direct_reply": OAUTH_OK}
            return {**noop, "kb_found": False, "direct_reply": f"Authorization failed: {exc}"}

    if not biz_user_id:
        return noop

    _warm_oauth_listener_if_needed(session, open_id=oid)

    if not _prefetch_on(config):
        return noop

    result = _query_knowledge(text, session, open_id=oid)
    if not result.get("success"):
        err = str(result.get("error") or "")
        err_l = err.lower()
        if any(x in err_l for x in ("oauth", "authorization", "user_access_token", "refresh")) or "not completed" in err_l:
            listener = _start_background_oauth_listener(session, open_id=oid)
            return {
                **noop,
                "kb_found": False,
                "direct_reply": _oauth_chat_reply(biz_user_id, open_id=oid, session=session, listener=listener),
            }
        if err.startswith("Feishu") or "feishu" in err_l:
            return {**noop, "kb_found": False, "direct_reply": err}
        return {**noop, "kb_found": False, "direct_reply": err or "Knowledge base query failed"}

    if not result.get("answer"):
        return {**noop, "kb_found": False}

    reply = _format_kb_reply(str(result["answer"]))
    if not reply:
        return {**noop, "kb_found": False}
    return {
        "api_message": message_text,
        "persist_user": message_text,
        "kb_found": True,
        "direct_reply": reply,
    }


def maybe_prefetch_knowledge_message(text, user_id, config, *, feishu_open_id="", feishu_raw_event=None):
    _load_dotenv()
    if user_id:
        os.environ["HERMES_SESSION_USER_ID"] = user_id
    out = gateway_prefetch(text, user_id, config, feishu_open_id=feishu_open_id, feishu_raw_event=feishu_raw_event)
    return out["api_message"], out["persist_user"], out["kb_found"], out["direct_reply"]


def _emit(ok: bool, **fields) -> None:
    print(json.dumps({"success": ok, **fields}, ensure_ascii=False), file=sys.stdout if ok else sys.stderr)


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Ask enterprise knowledge base")
    p.add_argument("--question", default="")
    p.add_argument("--biz-user-id", default="")
    p.add_argument("--open-id", default="")
    p.add_argument("--prefetch", action="store_true")
    p.add_argument("--authorize", action="store_true", help="Run OAuth (listen on redirect URI)")
    p.add_argument("--print-url", action="store_true", help="Only print OAuth URL, do not listen")
    p.add_argument("--timeout", type=float, default=180.0, help="OAuth listen timeout seconds")
    p.add_argument("--exchange-code", default="")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    sender = (args.open_id or args.biz_user_id or _session_user()).strip()
    open_id = args.open_id or (sender if sender.startswith("ou_") else "")

    if args.prefetch:
        print(json.dumps(gateway_prefetch(args.question, sender or _session_user(), None, feishu_open_id=open_id), ensure_ascii=False))
        return 0

    if args.status:
        users = (_token_store().get("users") or {})
        _emit(True, users=list(users.keys()) if isinstance(users, dict) else [])
        return 0

    if args.print_url:
        print(_authorize_url(open_id or sender))
        return 0

    if args.authorize:
        try:
            tok = _authorize_listen(
                sender or open_id or _session_user(),
                timeout=args.timeout,
                open_id=open_id,
            )
            _emit(True, user_key=sender or open_id, token_prefix=tok[:8])
        except RuntimeError as exc:
            _emit(False, error=str(exc))
            return 2
        return 0

    if args.exchange_code.strip():
        try:
            tok = _exchange_code(args.exchange_code.strip(), sender or _session_user(), open_id=open_id)
            _emit(True, user_key=sender, token_prefix=tok[:8])
        except RuntimeError as exc:
            _emit(False, error=str(exc))
            return 2
        return 0

    if not args.question.strip():
        _emit(False, error="Missing --question")
        return 2

    result = _ask(args.question, sender, open_id=open_id)
    if not result.get("success"):
        _emit(False, error=result["error"])
        err = str(result.get("error", "")).lower()
        return 2 if any(x in err for x in ("authorization", "oauth", "sender", "user_id", "feishu")) else 1
    if not result.get("found"):
        _emit(True, answer="", found=False)
        return 0
    _emit(True, answer=result["answer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
