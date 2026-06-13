"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False, context_length=None):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        import subprocess

        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        stable = _stable_prompt(agent)
        assert "coding agent" in stable
        assert "Workspace" in stable

    def test_absent_when_off(self, monkeypatch, tmp_path):
        import subprocess

        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        import subprocess

        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


class TestDatabaseQueryGuidance:
    """The answer-discipline contract is tool-gated guidance: present iff the
    database_query tool is loaded (mirrors MEMORY_GUIDANCE/COMPUTER_USE)."""

    def test_injected_when_database_query_loaded(self):
        stable = _stable_prompt(_make_agent(valid_tool_names=["database_query"]))
        assert "schema_sample" in stable
        assert "count_rows" in stable
        assert "clarify" in stable

    def test_absent_without_database_query(self):
        stable = _stable_prompt(_make_agent(valid_tool_names=[]))
        assert "schema_sample" not in stable or "count_rows" not in stable


class TestSkillAutoload:
    """A skill with `metadata.hermes.autoload: true` + `requires_toolsets` must
    have its BODY injected once the toolset is active. The model skips
    `skill_view` and just starts querying, so a workflow-governing skill
    (ask-data) has to be present at decision time, not fetched on demand."""

    def _write_skill(self, tmp_path, monkeypatch, autoload: bool):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        d = tmp_path / "skills" / "data-science" / "demo"
        d.mkdir(parents=True, exist_ok=True)
        flag = "    autoload: true\n" if autoload else ""
        (d / "SKILL.md").write_text(
            "---\nname: demo\ndescription: d\nmetadata:\n  hermes:\n"
            "    requires_toolsets: [database]\n" + flag + "---\n\nDEMO-METHOD-BODY\n",
            encoding="utf-8",
        )

    def test_body_injected_when_toolset_active(self, tmp_path, monkeypatch):
        from agent.prompt_builder import get_autoload_skill_bodies
        self._write_skill(tmp_path, monkeypatch, autoload=True)
        assert any("DEMO-METHOD-BODY" in b for b in get_autoload_skill_bodies({"database"}))

    def test_not_injected_without_toolset_or_flag(self, tmp_path, monkeypatch):
        from agent.prompt_builder import get_autoload_skill_bodies
        self._write_skill(tmp_path, monkeypatch, autoload=True)
        assert get_autoload_skill_bodies(set()) == []           # toolset not active
        self._write_skill(tmp_path, monkeypatch, autoload=False)  # flag removed
        assert get_autoload_skill_bodies({"database"}) == []
