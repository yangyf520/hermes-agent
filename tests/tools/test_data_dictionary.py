"""Tests for data dictionary path resolution and guidance injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.prompt_builder import DATABASE_QUERY_GUIDANCE, get_database_query_guidance
from tools import database_tool as db


def test_data_dictionary_root_from_env(tmp_path, monkeypatch):
    wiki = tmp_path / "mywiki"
    wiki.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    assert db.data_dictionary_root() == str(wiki.resolve())


def test_data_dictionary_root_default_home_wiki(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    wiki = home / "wiki"
    wiki.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.delenv("DB_WIKI_DIR", raising=False)
    assert db.data_dictionary_root() == str(wiki.resolve())


def test_get_database_query_guidance_includes_index(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# index\n")
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    out = get_database_query_guidance()
    assert DATABASE_QUERY_GUIDANCE in out
    assert str(wiki / "index.md") in out
    assert "read_file" in out
    assert "read at least one linked entity page" in out
    assert "column + code + label + source page" in out
    assert "requires_clarify" in out


def test_get_database_query_guidance_without_wiki(monkeypatch):
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.delenv("DB_WIKI_DIR", raising=False)
    monkeypatch.setattr(db, "data_dictionary_root", lambda: None)
    assert get_database_query_guidance() == DATABASE_QUERY_GUIDANCE
