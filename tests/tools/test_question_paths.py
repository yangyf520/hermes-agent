"""端到端路径测试：三个典型问数场景。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")

from tools import database_tool as db


@pytest.fixture
def it_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    url = f"sqlite+pysqlite:///{(tmp_path / 'it.sqlite3').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE Fact_IT_StStatus (
                id INTEGER PRIMARY KEY,
                lb_coordinator_code TEXT,
                lb_work_order_category TEXT,
                process_type TEXT,
                dt_start_date TEXT
            )
        """))
        c.execute(text("""
            CREATE TABLE Dim_Employee (
                id INTEGER PRIMARY KEY,
                lb_coordinator_code TEXT,
                lb_fullname TEXT
            )
        """))
        c.execute(text(
            "INSERT INTO Dim_Employee VALUES "
            "(1,'WB755','唐代智'),(2,'WB756','王小明'),(3,'WB757','李大强')"
        ))
        c.execute(text(
            "INSERT INTO Fact_IT_StStatus VALUES "
            "(1,'WB755','基础架构-网络服务','人工','2026-01-15'),"
            "(2,'WB755','应用-OA','人工','2026-02-10'),"
            "(3,'WB755','基础架构-网络服务','自动','2026-03-05'),"
            "(4,'WB756','基础架构-网络服务','人工','2026-01-20'),"
            "(5,'WB756','硬件-服务器','人工','2026-02-25'),"
            "(6,'WB755','基础架构-网络服务','人工','2025-12-31'),"
            "(7,'WB757','基础架构-网络服务','人工','2026-03-15'),"
            "(8,'WB757','基础架构-网络服务','自动','2026-04-10')"
        ))
    eng.dispose()
    monkeypatch.setenv("DB_DSN", url)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(wiki))

    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._WIKI_CODEBOOK_CACHE.clear()
    db._FAIL_COUNTS.clear()
    yield
    db._CACHED_ENGINE = db._CACHED_ENGINE_URL = None
    db._COLUMN_SCHEMA_CACHE.clear()
    db._WIKI_CODEBOOK_CACHE.clear()
    db._FAIL_COUNTS.clear()


def test_q1_person_count_path(it_db):
    json.loads(db.schema_sample(
        {"table": "Fact_IT_StStatus", "columns": ["lb_coordinator_code", "dt_start_date"]},
        task_id="q1",
    ))
    lookup = json.loads(db.database_query(
        {"sql": "SELECT lb_coordinator_code FROM Dim_Employee WHERE lb_fullname = '唐代智'"},
        task_id="q1",
    ))
    assert lookup["success"], lookup
    code = lookup["rows"][0]["lb_coordinator_code"]
    assert code == "WB755"

    c = json.loads(db.count_rows({
        "tables": [{
            "table": "Fact_IT_StStatus",
            "filters": [{"column": "lb_coordinator_code", "op": "=", "value": code}],
            "date_column": "dt_start_date",
            "start": "2026-01-01",
            "end": "2027-01-01",
        }]
    }, task_id="q1"))
    assert c["success"], c
    assert c["number"] == 3


def test_q2_category_count_path(it_db):
    json.loads(db.schema_sample(
        {"table": "Fact_IT_StStatus", "columns": ["lb_work_order_category", "dt_start_date"]},
        task_id="q2",
    ))
    c = json.loads(db.count_rows({
        "tables": [{
            "table": "Fact_IT_StStatus",
            "filters": [{"column": "lb_work_order_category", "op": "=", "value": "基础架构-网络服务"}],
            "date_column": "dt_start_date",
            "start": "2026-01-01",
            "end": "2027-01-01",
        }]
    }, task_id="q2"))
    assert c["success"], c
    assert c["number"] == 5


def test_q3_monthly_breakdown_path(it_db):
    sample = json.loads(db.schema_sample(
        {"table": "Fact_IT_StStatus", "columns": ["process_type"]},
        task_id="q3",
    ))
    assert sample["success"], sample
    assert "人工" in sample["columns"]["process_type"]["samples"]

    out = json.loads(db.database_query(
        {
            "sql": (
                "SELECT strftime('%Y-%m', dt_start_date) AS month, COUNT(*) AS cnt "
                "FROM Fact_IT_StStatus "
                "WHERE lb_coordinator_code IS NOT NULL AND dt_start_date >= '2026-01-01' "
                "GROUP BY strftime('%Y-%m', dt_start_date) "
                "ORDER BY month"
            ),
        },
        task_id="q3",
    ))
    assert out["success"], out
    rows = {r["month"]: r["cnt"] for r in out["rows"]}
    assert rows.get("2026-01") == 2
    assert rows.get("2026-02") == 2
    assert rows.get("2026-03") == 2
