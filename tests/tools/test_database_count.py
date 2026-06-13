"""Tests for count_rows and schema_sample."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")

from tools import database_tool as db  # noqa: E402


def _count(args, task_id=None):
    return json.loads(db.count_rows(args, task_id=task_id))


def _sample(args, task_id=None):
    return json.loads(db.schema_sample(args, task_id=task_id))


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    url = f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite3').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE sr (id INTEGER PRIMARY KEY, lb_first_class TEXT, dt_start_date TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE st ("
            "id INTEGER PRIMARY KEY, "
            "lb_work_order_category TEXT, "
            "lb_subclass_code TEXT, "
            "dt_start_date TEXT)"
        ))
        c.execute(text(
            "INSERT INTO sr VALUES "
            "(1,'1','2025-01-01'),(2,'1','2025-12-31'),(3,'0','2025-06-01')"
        ))
        c.execute(text(
            "INSERT INTO st VALUES "
            "(1,'基础架构-网络服务',NULL,'2025-02-01'),"
            "(2,'基础架构-网络服务',NULL,'2026-02-01'),"
            "(3,'应用-OA',NULL,'2025-03-01')"
        ))
    eng.dispose()
    monkeypatch.setenv("DB_DSN", url)
    wiki = tmp_path / "wiki"
    entities = wiki / "entities"
    entities.mkdir(parents=True)
    (entities / "fact-it-srstatus.md").write_text(
        "---\n"
        "title: Fact_IT_SrStatus\n"
        "table: sr\n"
        "---\n"
        "# Fact_IT_SrStatus\n"
        "服务请求事实表。\n"
        "- `lb_first_class`: 申请类别；选项=系统配置服务:0、网络服务:1、信息安全服务:2\n"
        "## Related Pages\n"
        "- [[Fact_IT_StStatus]]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WIKI_PATH", str(wiki))
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._WIKI_CODEBOOK_CACHE.clear()
    yield
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._WIKI_CODEBOOK_CACHE.clear()


def test_count_rows_single_table(sqlite_db):
    out = _count({
        "table_intent_confirmed": True,
        "tables": [{
            "table": "sr",
            "filters": [{"column": "lb_first_class", "op": "=", "value": "1"}],
            "date_column": "dt_start_date",
            "start": "2025-01-01",
            "end": "2026-01-01",
        }]
    }, task_id="t1")
    assert out["success"] is True
    assert out["number"] == 2
    assert out["tables"][0]["table_total_rows"] == 3
    assert "final_sql" in out
    assert "COUNT" in out["final_sql"].upper()
    evidence = out["filter_evidence"]["dictionary_matches"][0]
    assert evidence["column"] == "lb_first_class"
    assert evidence["value"] == "1"
    assert evidence["label"] == "网络服务"
    assert evidence["source_file"].endswith("fact-it-srstatus.md")
    assert "选项=系统配置服务:0、网络服务:1" in evidence["source_line"]


def test_count_rows_multi_table(sqlite_db):
    out = _count({
        "table_intent_confirmed": True,
        "tables": [
            {"table": "sr", "filters": [{"column": "lb_first_class", "op": "=", "value": "1"}]},
            {"table": "st", "filters": [{"column": "lb_work_order_category", "op": "=", "value": "应用-OA"}]},
        ]
    }, task_id="t2")
    assert out["success"] is True
    assert out["number"] == 3


def test_count_rows_no_filters(sqlite_db):
    out = _count({
        "table_intent_confirmed": True,
        "tables": [{"table": "sr"}],
    })
    assert out["success"] is True
    assert out["number"] == 3


def test_count_rows_zero_has_diagnostic(sqlite_db):
    out = _count({
        "tables": [{
            "table": "st",
            "filters": [{"column": "lb_work_order_category", "op": "=", "value": "NOPE"}],
        }]
    }, task_id="t3")
    assert out["success"] is True
    assert out["number"] == 0
    assert "zero_count_diagnostic" in out
    samples = out["zero_count_diagnostic"]["st"]["filter_column_samples"]["lb_work_order_category"]
    assert "基础架构-网络服务" in samples


def test_count_rows_mismatch_codebook_value_warns(sqlite_db):
    out = _count({
        "table_intent_confirmed": True,
        "tables": [{
            "table": "sr",
            "filters": [{"column": "lb_first_class", "op": "=", "value": "999"}],
        }]
    }, task_id="t-mismatch")
    assert out["success"] is True
    assert out["filter_evidence"]["dictionary_mismatches"]
    assert out.get("evidence_warning")


def test_count_rows_requires_clarify_when_no_codebook(sqlite_db):
    out = _count({
        "tables": [{
            "table": "st",
            "filters": [{"column": "lb_work_order_category", "op": "=", "value": "基础架构-网络服务"}],
        }]
    }, task_id="t-requires-clarify")
    assert out["success"] is True
    assert out["filter_evidence"]["coded_filter_count"] >= 1
    assert out["filter_evidence"]["requires_clarify"] is True
    assert out.get("requires_clarify") is True


def test_count_rows_has_table_selection_hints(sqlite_db):
    out = _count({
        "tables": [{
            "table": "sr",
            "filters": [{"column": "lb_first_class", "op": "=", "value": "1"}],
        }]
    }, task_id="t-table-hints")
    assert out["success"] is False
    hints = out.get("table_selection_hints") or {}
    assert hints.get("selected_tables")
    assert hints.get("alternative_candidates")
    assert hints.get("requires_clarify") is True
    assert out.get("blocked") is True
    assert out.get("hard_stop") is True
    assert out.get("retryable") is False
    assert out.get("requires_clarify") is True
    assert out.get("next_action") == "clarify"
    assert "number" not in out
    assert "tables" not in out


def test_count_rows_confirmed_intent_allows_execution(sqlite_db):
    out = _count({
        "table_intent_confirmed": True,
        "tables": [{
            "table": "sr",
            "filters": [{"column": "lb_first_class", "op": "=", "value": "1"}],
        }]
    }, task_id="t-confirmed")
    assert out["success"] is True
    assert out["number"] == 2


def test_schema_sample_all_columns(sqlite_db):
    out = _sample({"table": "sr"})
    assert out["success"] is True
    assert "lb_first_class" in out["columns"]
    assert "samples" in out["columns"]["lb_first_class"]


def test_schema_sample_specific_columns(sqlite_db):
    out = _sample({"table": "st", "columns": ["lb_work_order_category"]})
    assert out["success"] is True
    assert "基础架构-网络服务" in out["columns"]["lb_work_order_category"]["samples"]
    dist = out["columns"]["lb_work_order_category"].get("value_distribution")
    assert dist is not None
    assert any(v["value"] == "基础架构-网络服务" for v in dist["top_values"])


def test_schema_sample_unknown_table(sqlite_db):
    out = _sample({"table": "no_such_table"})
    assert out["success"] is False


def test_schema_sample_invalid_ident(sqlite_db):
    out = _sample({"table": "sr; DROP TABLE sr"})
    assert out["success"] is False
