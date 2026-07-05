"""ask-knowledge: OAuth, gateway prefetch, FAQ/data routing (mocked)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/productivity/ask-knowledge/scripts/ask_knowledge.py"
FAQ_Q = "licenseca怎么使用"
DATA_Q = "今年网络类的问题一共有多少？"
DUAL_CFG = {
    "platform_toolsets": {"feishu": ["hermes-feishu", "database"]},
    "knowledge": {"prefetch": True},
}
FAQ_CFG = {"platform_toolsets": {"feishu": ["hermes-feishu"]}, "knowledge": {"prefetch": True}}


def _load_ak(monkeypatch, *, home: str = "/tmp/hermes-test"):
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("KNOWLEDGE_APP_ID", "app")
    monkeypatch.setenv("KNOWLEDGE_SKILL_ID", "skill_1")
    monkeypatch.setenv("KNOWLEDGE_CLIENT_APP_ID", "cli")
    monkeypatch.setenv("KNOWLEDGE_CLIENT_APP_SECRET", "sec")
    spec = importlib.util.spec_from_file_location("ask_knowledge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ak(monkeypatch):
    return _load_ak(monkeypatch)


def test_oauth_exchange_stores_per_user(ak, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ak, "_oauth_cache_path", lambda: tmp_path / "oauth.json")
    monkeypatch.setattr(sys, "argv", ["ask_knowledge.py", "--exchange-code", "c", "--biz-user-id", "12211"])
    with patch.object(ak, "_oauth_post", return_value={
        "code": 0, "data": {"access_token": "at", "refresh_token": "rt", "expires_in": 7200},
    }):
        assert ak.main() == 0
    saved = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert saved["users"]["12211"]["access_token"] == "at"
    assert saved["users"]["12211"]["refresh_token"] == "rt"


def test_ask_with_token_returns_answer(ak, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "12211")
    monkeypatch.setattr(sys, "argv", ["ask_knowledge.py", "--question", "q"])
    with patch.object(ak, "_user_access_token_detail", return_value=("tok", "")), patch.object(
        ak, "_http", return_value={"data": {"output": "KB answer"}},
    ):
        assert ak.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["answer"] == "KB answer"


def test_ask_without_oauth_fails(ak, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "12211")
    monkeypatch.setattr(sys, "argv", ["ask_knowledge.py", "--question", "q"])
    with patch.object(ak, "_user_access_token_detail", return_value=("", "")):
        assert ak.main() == 2
    err = json.loads(capsys.readouterr().err)
    assert "authorization" in err["error"].lower() or "oauth" in err["error"].lower()


def test_prefetch_kb_hit_and_oauth_prompt(ak):
    with patch.object(ak, "_user_access_token_detail", return_value=("tok", "")), patch.object(
        ak, "_query_knowledge", return_value={"success": True, "answer": "hit", "found": True},
    ), patch.object(ak, "_format_kb_reply", return_value="formatted hit"):
        hit = ak.gateway_prefetch("question", "12211", FAQ_CFG)
    assert hit["kb_found"] is True and hit["direct_reply"] == "formatted hit"

    with patch.object(ak, "_user_access_token_detail", return_value=("", "")), patch.object(
        ak, "_query_knowledge", return_value={
            "success": False, "error": "User 12211 has not completed Feishu OAuth authorization",
        },
    ), patch.object(ak, "_start_background_oauth_listener", return_value=True) as start_listener:
        miss = ak.gateway_prefetch("question", "12211", FAQ_CFG)
    assert start_listener.call_count >= 1
    assert miss["kb_found"] is False and miss["direct_reply"] and "http" in miss["direct_reply"]
    assert "listener on" in miss["direct_reply"].lower()


def test_database_toolset_disables_kb_prefetch(ak):
    """Config split: database on feishu disables KB prefetch (agent routes 问数)."""
    from hermes_cli.tools_config import _get_platform_tools

    assert "database" in _get_platform_tools(DUAL_CFG, "feishu")
    assert not ak._prefetch_on(DUAL_CFG)

    with patch.object(ak, "_query_knowledge") as qk, patch.object(
        ak, "_user_access_token_detail", return_value=("tok", ""),
    ):
        for q in (DATA_Q, FAQ_Q):
            out = ak.gateway_prefetch(q, "12211", DUAL_CFG)
            assert out["kb_found"] is None and out["direct_reply"] is None
    qk.assert_not_called()


def test_dual_config_warms_oauth_listener_without_prefetch(ak):
    with patch.object(ak, "_user_access_token_detail", return_value=("", "")), patch.object(
        ak, "_start_background_oauth_listener", return_value=True,
    ) as start_listener:
        out = ak.gateway_prefetch(FAQ_Q, "12211", DUAL_CFG)
    start_listener.assert_called_once()
    assert out["kb_found"] is None and out["direct_reply"] is None


def test_faq_only_toolset_still_prefetches(ak):
    assert ak._prefetch_on(FAQ_CFG)
    with patch.object(ak, "_query_knowledge", return_value={"success": True, "answer": "faq", "found": True}), patch.object(
        ak, "_format_kb_reply", return_value="formatted FAQ",
    ):
        out = ak.gateway_prefetch(FAQ_Q, "12211", FAQ_CFG)
    assert out["kb_found"] is True and out["direct_reply"] == "formatted FAQ"


def test_oauth_callback_works_when_prefetch_disabled(ak):
    with patch.object(ak, "_exchange_code", return_value="tok") as exchange:
        out = ak.gateway_prefetch("http://127.0.0.1:8765/callback?code=abc", "12211", DUAL_CFG)
    exchange.assert_called_once()
    assert out["direct_reply"] == ak.OAUTH_OK


def test_oauth_callback_reused_code_ok_when_token_valid(ak):
    url = "http://127.0.0.1:8765/callback?code=used&state=ou_test"
    with patch.object(ak, "_exchange_code", side_effect=RuntimeError("code used")), patch.object(
        ak, "_user_access_token_detail", return_value=("tok", ""),
    ):
        out = ak.gateway_prefetch(url, "12211", DUAL_CFG, feishu_open_id="ou_test")
    assert out["direct_reply"] == ak.OAUTH_OK


def test_oauth_callback_without_biz_user_id(ak):
    url = "http://127.0.0.1:8765/callback?code=abc&state=ou_test"
    with patch.object(ak, "_exchange_code", return_value="tok") as exchange:
        out = ak.gateway_prefetch(url, "", DUAL_CFG)
    exchange.assert_called_once()
    assert out["direct_reply"] == ak.OAUTH_OK
