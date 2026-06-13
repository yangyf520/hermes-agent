"""database_query behavior tests."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")

from tools import database_tool as db  # noqa: E402


def _q(args, task_id=None):
    return json.loads(db.database_query(args, task_id=task_id))


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    url = f"sqlite+pysqlite:///{(tmp_path / 'dbq.sqlite3').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, status TEXT, coordinator TEXT)"))
        c.execute(text("INSERT INTO orders VALUES (1,100,'paid','WB001'),(2,50,'pending','WB002')"))
    eng.dispose()
    monkeypatch.setenv("DB_DSN", url)
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._FAIL_COUNTS.clear()
    yield
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._FAIL_COUNTS.clear()


def test_non_class_like_queries_run(sqlite_db):
    out = _q({"sql": "SELECT id FROM orders ORDER BY id", "max_rows": 1})
    assert out["success"] and out["row_count"] == 1 and out["truncated"]


def test_group_by_sql_runs_without_gate(sqlite_db):
    out = _q(
        {"sql": "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"},
        task_id="t-analysis",
    )
    assert out["success"] is True, out
    assert out["row_count"] == 2


def test_filtered_sql_runs_without_gate(sqlite_db):
    out = _q(
        {"sql": "SELECT COUNT(*) AS c FROM orders WHERE status = 'paid'"},
        task_id="filtered",
    )
    assert out["success"] is True
    assert out["rows"][0]["c"] == 1


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "SELECT id FROM orders; SELECT id FROM orders",
    "SELECT id INTO outfile '/tmp/x' FROM orders",
    "   ",
])
def test_reject_unsafe_sql(sqlite_db, sql):
    assert _q({"sql": sql})["success"] is False


def test_column_error_has_columns(sqlite_db):
    out = _q({"sql": "SELECT bogus_col FROM orders"})
    assert out["success"] is False
    assert sorted(out["evidence"]["available_columns"]["orders"]) == ["amount", "coordinator", "id", "status"]


def test_table_error_has_candidates(sqlite_db):
    out = _q({"sql": "SELECT id FROM ordrs"})
    assert out["success"] is False
    assert "orders" in out["evidence"]["candidate_tables"]


def test_zero_rows_no_auto_followup(sqlite_db):
    out = _q({"sql": "SELECT * FROM orders WHERE status = 'NOPE'"})
    assert out["success"] and out["row_count"] == 0
    assert "evidence" not in out


def test_non_class_like_queries_run(sqlite_db):
    out = _q({"sql": "SELECT DISTINCT status FROM orders WHERE status LIKE '%pa%'"})
    assert out["success"] is True
    assert "LIKE" in out["sql"].upper()


def test_fail_streak_hard_stop(sqlite_db):
    for _ in range(2):
        assert _q({"sql": "SELECT bogus FROM orders"}, task_id="t").get("hard_stop") is not True
    out3 = _q({"sql": "SELECT bogus FROM orders"}, task_id="t")
    assert out3.get("hard_stop") is True and "clarify" in out3["error"].lower()


def test_unfiltered_group_by_allowed(sqlite_db):
    out = _q(
        {"sql": "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"},
        task_id="explore",
    )
    assert out["success"] is True, out


@pytest.fixture
def ststatus_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    url = f"sqlite+pysqlite:///{(tmp_path / 'st.sqlite3').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE Fact_IT_StStatus (
                id INTEGER PRIMARY KEY,
                lb_it_coordinator_code TEXT,
                lb_process_status_code TEXT,
                dt_start_date TEXT
            )
        """))
        c.execute(text(
            "INSERT INTO Fact_IT_StStatus VALUES "
            "(1,'WB001','2','2026-01-15'),(2,NULL,'2','2026-01-20'),"
            "(3,'WB002','1','2026-02-10'),(4,'WB003','2','2026-02-25')"
        ))
    eng.dispose()
    monkeypatch.setenv("DB_DSN", url)
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._FAIL_COUNTS.clear()
    yield
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._FAIL_COUNTS.clear()


def test_schema_sample_reveals_codes(ststatus_db):
    out = json.loads(db.schema_sample(
        {"table": "Fact_IT_StStatus", "columns": ["lb_it_coordinator_code"]},
        task_id="mp",
    ))
    assert out["success"]
    assert "WB001" in json.dumps(out, ensure_ascii=False)
