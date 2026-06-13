"""Database tooling: schema_sample · count_rows · database_query."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any
from urllib.parse import quote_plus

from tools.registry import registry

logger = logging.getLogger(__name__)


# ── write-guard ───────────────────────────────────────────────────────────────
_DENY_RW = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|CALL|EXECUTE|EXEC)\b",
    re.IGNORECASE,
)

# ── dialect map ───────────────────────────────────────────────────────────────
_DB_KIND = {
    "postgres": "pg", "postgresql": "pg", "pg": "pg",
    "mysql": "mysql",
    "sqlserver": "mssql", "mssql": "mssql",
}

# ── identifiers & ops ─────────────────────────────────────────────────────────
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_OPS = {"=", "!=", "<>", ">", ">=", "<", "<=", "IN", "NOT IN"}

# ── SQL parsing (error hints) ───────────────────────────────────────────────────
_RE_FROM_TABLE = re.compile(r"\bFROM\s+([A-Za-z_][\w]*)", re.IGNORECASE)

# ── error message patterns ────────────────────────────────────────────────────
_RE_INVALID_COL = re.compile(
    r"[Ii]nvalid column name '([^']+)'"
    r"|[Uu]nknown column '([^']+)'"
    r"|column \"([^\"]+)\" does not exist"
    r"|no such column:\s*(\S+)"
)

_PROBE_ROW_N = 5
_PROBE_FIELD_LIMIT = 5

# ── config ────────────────────────────────────────────────────────────────────
_SAMPLE_N = 12

# ── caches & task state ───────────────────────────────────────────────────────
_ENGINE_LOCK = threading.Lock()
_CACHED_ENGINE: Any | None = None
_CACHED_ENGINE_URL: str | None = None

_COLUMN_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_WIKI_CODEBOOK_CACHE: dict[str, dict[str, Any]] = {}

_FAIL_COUNTS: dict[str, int] = {}
_FAIL_LIMIT = 3


def data_dictionary_root() -> str | None:
    """Wiki/dictionary directory from WIKI_PATH, DB_WIKI_DIR, or ~/wiki if present."""
    for key in ("WIKI_PATH", "DB_WIKI_DIR"):
        raw = (os.getenv(key) or "").strip()
        if not raw:
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            return path
    default = os.path.abspath(os.path.expanduser("~/wiki"))
    if os.path.isdir(default):
        return default
    return None


def _is_coded_filter_column(column: str) -> bool:
    c = str(column or "").lower()
    if not c:
        return False
    hints = ("class", "category", "subclass", "status", "type", "code")
    return c.startswith("lb_") and any(h in c for h in hints)


def _parse_frontmatter(markdown_text: str) -> dict[str, str]:
    text = str(markdown_text or "")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fm = text[4:end]
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _parse_codebook_pairs(option_text: str) -> dict[str, str]:
    """Parse wiki codebook text like: 网络服务:1、信息安全服务:2."""
    text = str(option_text or "").strip()
    if not text:
        return {}
    pairs: dict[str, str] = {}
    for m in re.finditer(r"([^:：、，,]+)\s*[:：]\s*([^、，,]+)", text):
        label = m.group(1).strip()
        code = m.group(2).strip().rstrip("。.;；")
        if label and code:
            pairs[code] = label
    return pairs


def _load_table_codebooks(table: str) -> dict[str, Any]:
    table_key = str(table or "").strip()
    if not table_key:
        return {"columns": {}, "source_file": None}
    cached = _WIKI_CODEBOOK_CACHE.get(table_key.lower())
    if cached is not None:
        return cached

    root = data_dictionary_root()
    if not root:
        out = {"columns": {}, "source_file": None}
        _WIKI_CODEBOOK_CACHE[table_key.lower()] = out
        return out

    entities_dir = os.path.join(root, "entities")
    if not os.path.isdir(entities_dir):
        out = {"columns": {}, "source_file": None}
        _WIKI_CODEBOOK_CACHE[table_key.lower()] = out
        return out

    target_file: str | None = None
    for name in sorted(os.listdir(entities_dir)):
        if not name.lower().endswith(".md"):
            continue
        p = os.path.join(entities_dir, name)
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            fm = _parse_frontmatter(txt)
            fm_table = str(fm.get("table", "")).strip()
            if fm_table and fm_table.lower() == table_key.lower():
                target_file = p
                break
        except Exception:
            continue

    if not target_file:
        out = {"columns": {}, "source_file": None}
        _WIKI_CODEBOOK_CACHE[table_key.lower()] = out
        return out

    columns: dict[str, dict[str, Any]] = {}
    related_tables: list[str] = []
    description_excerpt = ""
    try:
        with open(target_file, encoding="utf-8") as f:
            body = f.read()
        for m in re.finditer(
            r"^\s*-\s*`(?P<col>[^`]+)`\s*:[^\n]*?选项=(?P<opts>.+)$",
            body,
            re.MULTILINE,
        ):
            col = m.group("col").strip()
            full_line = m.group(0).strip()
            pairs = _parse_codebook_pairs(m.group("opts"))
            if pairs:
                columns[col] = {"code_to_label": pairs, "source_line": full_line}
        lines = body.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith("# "):
                for cand in lines[i + 1 :]:
                    s = cand.strip()
                    if not s:
                        continue
                    if s.startswith("## "):
                        break
                    description_excerpt = s
                    break
                break
        for m in re.finditer(r"\[\[([^\]]+)\]\]", body):
            name = m.group(1).strip()
            if name and name.lower().startswith("fact_"):
                related_tables.append(name)
    except Exception:
        pass

    out = {
        "columns": columns,
        "source_file": target_file,
        "related_tables": sorted(set(related_tables)),
        "description_excerpt": description_excerpt,
    }
    _WIKI_CODEBOOK_CACHE[table_key.lower()] = out
    return out


def _table_selection_hints(specs: list[dict[str, Any]]) -> dict[str, Any]:
    selected = []
    alternatives = []
    selected_names = {
        str(spec.get("table") or "").strip().lower()
        for spec in specs
        if isinstance(spec, dict)
    }
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        table = str(spec.get("table") or "").strip()
        if not table:
            continue
        profile = _load_table_codebooks(table)
        selected.append(
            {
                "table": table,
                "source_file": profile.get("source_file"),
                "description_excerpt": profile.get("description_excerpt", ""),
            }
        )
        for rel in (profile.get("related_tables") or []):
            rel_norm = str(rel).strip().lower()
            if rel_norm and rel_norm not in selected_names:
                alternatives.append(
                    {
                        "for_table": table,
                        "candidate_table": rel,
                        "reason": "Related fact table exists in dictionary; verify intent before finalizing table choice.",
                    }
                )
    alternatives = sorted(
        {f"{a['for_table']}::{a['candidate_table']}": a for a in alternatives}.values(),
        key=lambda x: (x["for_table"], x["candidate_table"]),
    )
    return {
        "selected_tables": selected,
        "alternative_candidates": alternatives,
        "requires_clarify": bool(alternatives),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Connection
# ═══════════════════════════════════════════════════════════════════════════════

def _legacy_connection_url() -> str:
    kind = (os.getenv("DB_TYPE") or "").strip().lower()
    host = (os.getenv("DB_HOST") or "").strip()
    user = (os.getenv("DB_USERNAME") or "").strip()
    password = (os.getenv("DB_PASSWORD") or "").strip()
    port_raw = (os.getenv("DB_PORT") or "").strip()
    name = (os.getenv("DB_NAME") or "").strip()

    canon = _DB_KIND.get(kind)
    if not canon or not host or not user or not password:
        return ""
    port = port_raw if port_raw.isdigit() else {"pg": "5432", "mysql": "3306", "mssql": "1433"}[canon]
    u, p = quote_plus(user), quote_plus(password)
    if canon == "pg":
        return f"postgresql+psycopg://{u}:{p}@{host}:{port}/{name or 'postgres'}"
    if canon == "mysql":
        return f"mysql+pymysql://{u}:{p}@{host}:{port}/{name or 'mysql'}"
    if not name:
        return ""
    return f"mssql+pytds://{u}:{p}@{host}:{port}/{name}"


def _connection_url() -> str:
    for key in ("DB_DSN", "DATABASE_URL"):
        v = (os.getenv(key) or "").strip()
        if v:
            return v
    return _legacy_connection_url()


def _dialect_kind() -> str:
    kind = (os.getenv("DB_TYPE") or "").strip().lower()
    dsn = (_connection_url() or "").lower()
    if kind in ("postgres", "postgresql", "pg") or "postgresql" in dsn or "psycopg" in dsn:
        return "pg"
    if kind == "mysql" or "mysql" in dsn or "pymysql" in dsn:
        return "mysql"
    if kind == "sqlite" or "sqlite" in dsn:
        return "sqlite"
    if kind in ("sqlserver", "mssql") or "mssql" in dsn or "pytds" in dsn:
        return "mssql"
    return ""


def configured() -> bool:
    return bool(_connection_url())


# ═══════════════════════════════════════════════════════════════════════════════
# Core SQL execution
# ═══════════════════════════════════════════════════════════════════════════════

def _engine():
    global _CACHED_ENGINE, _CACHED_ENGINE_URL
    url = _connection_url()
    if not url:
        raise RuntimeError(
            "Database config missing. Set DB_DSN/DATABASE_URL, or DB_TYPE+DB_HOST+DB_USERNAME+DB_PASSWORD."
        )
    from sqlalchemy import create_engine
    with _ENGINE_LOCK:
        if _CACHED_ENGINE is not None and _CACHED_ENGINE_URL == url:
            return _CACHED_ENGINE
        if _CACHED_ENGINE is not None:
            try:
                _CACHED_ENGINE.dispose()
            except Exception:
                pass
        _CACHED_ENGINE_URL = url
        _CACHED_ENGINE = create_engine(url, pool_pre_ping=True)
        return _CACHED_ENGINE


def _run_select(core: str, max_rows: int) -> tuple[list[dict[str, Any]], bool, bool]:
    from sqlalchemy import text
    with _engine().connect() as conn:
        r = conn.execute(text(core))
        if not r.returns_rows:
            return [{"rowcount": getattr(r, "rowcount", None), "returns_rows": False}], False, False
        chunk = r.mappings().fetchmany(max_rows + 1)
        cut = len(chunk) > max_rows
        rows = [dict(m) for m in (chunk[:max_rows] if cut else chunk)]
        return rows, True, cut


# ═══════════════════════════════════════════════════════════════════════════════
# Schema helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _columns_for(table: str) -> list[str]:
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            if _dialect_kind() == "sqlite":
                return [str(r[1]) for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
            return [str(r[0]) for r in conn.execute(
                text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"),
                {"t": table},
            ).fetchall()]
    except Exception:
        return []


def _list_tables() -> list[str]:
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            if _dialect_kind() == "sqlite":
                rows = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )).fetchall()
                return [str(r[0]) for r in rows]
            rows = conn.execute(text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
            )).fetchall()
            return [str(r[0]) for r in rows]
    except Exception:
        return []


def _similar_tables(name: str, tables: list[str]) -> list[str]:
    from difflib import get_close_matches
    return get_close_matches(name, tables, n=5, cutoff=0.5)


def _sample_distinct(table: str, col: str, n: int = _SAMPLE_N) -> list[str]:
    dk = _dialect_kind()
    cap = f"TOP {n} " if dk == "mssql" else ""
    tail = "" if dk == "mssql" else f" LIMIT {n}"
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(
                text(f"SELECT DISTINCT {cap}{col} AS v FROM {table} WHERE {col} IS NOT NULL{tail}")
            ).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def _value_distribution(table: str, col: str, n: int = 10) -> dict[str, Any]:
    """Top-N value counts + null share — evidence for qualifier grounding."""
    dk = _dialect_kind()
    top = f"TOP {n} " if dk == "mssql" else ""
    tail = "" if dk == "mssql" else f" LIMIT {n}"
    dist: list[dict[str, Any]] = []
    null_pct: float | None = None
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(text(
                f"SELECT {top}{col} AS v, COUNT(*) AS cnt FROM {table} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY cnt DESC{tail}"
            )).fetchall()
            dist = [{"value": str(r[0]), "count": int(r[1])} for r in rows]
            nr = conn.execute(text(
                f"SELECT SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls, "
                f"COUNT(*) AS total FROM {table}"
            )).fetchone()
            if nr and nr[1]:
                null_pct = round(100.0 * float(nr[0] or 0) / float(nr[1]), 1)
    except Exception:
        pass
    return {"top_values": dist, "null_pct": null_pct}


def _sample_row_examples(table: str, cols: list[str], n: int = _PROBE_ROW_N) -> list[dict[str, Any]]:
    """Fetch n rows where all listed columns are non-null — learn storage format."""
    if not cols:
        return []
    dk = _dialect_kind()
    col_list = ", ".join(cols)
    where = " AND ".join(f"{c} IS NOT NULL AND {c} != ''" for c in cols)
    top = f"TOP {n} " if dk == "mssql" else ""
    tail = "" if dk == "mssql" else f" LIMIT {n}"
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(
                text(f"SELECT {top}{col_list} FROM {table} WHERE {where}{tail}")
            ).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _column_formats(table: str, cols: list[str], *, profile: bool = False) -> dict[str, Any]:
    cache = _COLUMN_SCHEMA_CACHE.setdefault(table, {})
    missing = [c for c in cols if c not in cache]
    if missing:
        types: dict[str, str] = {}
        if _dialect_kind() != "sqlite":
            try:
                from sqlalchemy import text
                with _engine().connect() as conn:
                    rows = conn.execute(
                        text("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"),
                        {"t": table},
                    ).fetchall()
                types = {str(r[0]): str(r[1]) for r in rows}
            except Exception:
                pass
        cache.update({
            col: {
                "data_type": types.get(col, ""),
                "samples": _sample_distinct(table, col)[:8],
                **({"value_distribution": _value_distribution(table, col)} if profile else {}),
            }
            for col in missing
        })
    return {c: cache[c] for c in cols if c in cache}


def _fail(task_id: str | None, payload: dict[str, Any]) -> str:
    key = task_id or "_global"
    _FAIL_COUNTS[key] = _FAIL_COUNTS.get(key, 0) + 1
    if _FAIL_COUNTS[key] >= _FAIL_LIMIT:
        payload["hard_stop"] = True
        payload["error"] = (
            f"Hard stop: {_FAIL_LIMIT} consecutive database_query failures on this task. "
            "Stop guessing and call clarify."
        )
    return json.dumps({"success": False, **payload}, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
# SQL building & COUNT execution
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_ident(name: str, *, kind: str) -> str:
    n = str(name or "").strip()
    if not _SAFE_IDENT.match(n):
        raise ValueError(f"Invalid {kind}: {name!r}")
    return n


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).replace("'", "''")
    if _dialect_kind() == "mssql":
        return f"N'{s}'"
    return f"'{s}'"


def _build_filter_clause(filters: list[dict[str, Any]]) -> str:
    terms: list[str] = []
    for f in filters:
        if not isinstance(f, dict):
            raise ValueError("Each filter must be an object.")
        col = _safe_ident(f.get("column", ""), kind="filter column")
        op = str(f.get("op", "=")).strip().upper()
        if op not in _ALLOWED_OPS:
            raise ValueError(f"Unsupported filter operator: {op}")
        value = f.get("value")
        if op in {"IN", "NOT IN"}:
            if not isinstance(value, list) or not value:
                raise ValueError(f"{op} filter requires non-empty list value.")
            vals = ", ".join(_sql_literal(v) for v in value)
            terms.append(f"{col} {op} ({vals})")
        else:
            if value is None:
                if op in {"=", "=="}:
                    terms.append(f"{col} IS NULL")
                elif op in {"!=", "<>"}:
                    terms.append(f"{col} IS NOT NULL")
                else:
                    raise ValueError(f"Operator {op} cannot be used with NULL.")
            else:
                terms.append(f"{col} {op} {_sql_literal(value)}")
    return " AND ".join(terms)


def _count_one_table(spec: dict[str, Any]) -> dict[str, Any]:
    table = _safe_ident(spec.get("table", ""), kind="table")
    filters = spec.get("filters") or []
    if not isinstance(filters, list):
        raise ValueError("`filters` must be a list.")

    date_col_raw = spec.get("date_column")
    start = spec.get("start")
    end = spec.get("end")
    terms: list[str] = []

    f_clause = _build_filter_clause(filters)
    if f_clause:
        terms.append(f_clause)

    date_col = ""
    if date_col_raw:
        date_col = _safe_ident(date_col_raw, kind="date_column")
        if start is not None:
            terms.append(f"{date_col} >= {_sql_literal(start)}")
        if end is not None:
            terms.append(f"{date_col} < {_sql_literal(end)}")

    where = (" WHERE " + " AND ".join(terms)) if terms else ""
    sql = f"SELECT COUNT(*) AS cnt FROM {table}{where}"
    rows, _returns_rows, _cut = _run_select(sql, 2)
    cnt = int((rows[0] or {}).get("cnt") or 0) if rows else 0

    coverage = None
    if date_col:
        cov_sql = f"SELECT MIN({date_col}) AS lo, MAX({date_col}) AS hi FROM {table}"
        cov_rows, _r2, _c2 = _run_select(cov_sql, 2)
        if cov_rows:
            coverage = {"date_column": date_col, "min": cov_rows[0].get("lo"), "max": cov_rows[0].get("hi")}

    total_sql = f"SELECT COUNT(*) AS cnt FROM {table}"
    total_rows, _tr, _tc = _run_select(total_sql, 2)
    table_total = int((total_rows[0] or {}).get("cnt") or 0) if total_rows else 0

    return {
        "table": table,
        "count_sql": sql,
        "count": cnt,
        "table_total_rows": table_total,
        "coverage": coverage,
    }


def _combined_final_sql(per_table: list[dict[str, Any]]) -> str:
    selects = []
    for t in per_table:
        table_label = str(t["table"]).replace("'", "''")
        count_sql = str(t["count_sql"])
        selects.append(f"SELECT '{table_label}' AS table_name, ({count_sql}) AS cnt")
    if len(selects) == 1:
        return per_table[0]["count_sql"]
    return "SELECT SUM(cnt) AS total_count FROM (\n  " + "\n  UNION ALL\n  ".join(selects) + "\n) AS counts"


def _zero_count_diagnostic(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Sample filter columns so the model can diagnose why COUNT=0."""
    diag: dict[str, Any] = {}
    for spec in specs:
        table = str(spec.get("table") or "").strip()
        if not table or not _SAFE_IDENT.match(table):
            continue
        filter_cols: list[str] = []
        for f in spec.get("filters") or []:
            if isinstance(f, dict) and f.get("column") and _SAFE_IDENT.match(str(f["column"])):
                filter_cols.append(str(f["column"]))
        dc = spec.get("date_column")
        if dc and _SAFE_IDENT.match(str(dc)):
            filter_cols.append(str(dc))
        filter_cols = list(dict.fromkeys(filter_cols))[:4]
        if not filter_cols:
            continue
        try:
            from sqlalchemy import text as _text
            samples = {col: _sample_distinct(table, col, n=5) for col in filter_cols}
            cov: dict[str, Any] = {}
            with _engine().connect() as conn:
                for col in filter_cols:
                    r = conn.execute(_text(f"SELECT MIN({col}) AS lo, MAX({col}) AS hi FROM {table}")).fetchone()
                    if r and (r[0] is not None or r[1] is not None):
                        cov[col] = {"min": str(r[0]), "max": str(r[1])}
            diag[table] = {"filter_column_samples": samples, "coverage": cov}
        except Exception:
            pass
    return diag


def _dictionary_filter_evidence(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return wiki-backed codebook evidence for coded filters."""
    matches: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    coded_filter_count = 0

    for spec in specs:
        table = str(spec.get("table") or "").strip()
        if not table or not _SAFE_IDENT.match(table):
            continue
        codebooks = _load_table_codebooks(table)
        by_col = codebooks.get("columns") or {}
        source_file = codebooks.get("source_file")
        for f in spec.get("filters") or []:
            if not isinstance(f, dict):
                continue
            col = str(f.get("column") or "").strip()
            if not col or not _is_coded_filter_column(col):
                continue
            coded_filter_count += 1
            val = f.get("value")
            val_s = str(val).strip() if val is not None else ""
            if not val_s:
                continue
            col_meta = by_col.get(col) or {}
            mapping = (col_meta.get("code_to_label") or {})
            source_line = str(col_meta.get("source_line") or "")
            if not mapping:
                unverified.append(
                    {
                        "table": table,
                        "column": col,
                        "value": val_s,
                        "reason": "No codebook mapping found in wiki for this column.",
                    }
                )
                continue
            label = mapping.get(val_s)
            if label is None:
                mismatches.append(
                    {
                        "table": table,
                        "column": col,
                        "value": val_s,
                        "reason": "Value not found in wiki codebook for this column.",
                        "known_codes": sorted(mapping.keys())[:20],
                    }
                )
                continue
            matches.append(
                {
                    "table": table,
                    "column": col,
                    "value": val_s,
                    "label": label,
                    "source_file": source_file,
                    "source_line": source_line,
                }
            )

    return {
        "coded_filter_count": coded_filter_count,
        "dictionary_matches": matches,
        "dictionary_mismatches": mismatches,
        "unverified_coded_filters": unverified,
        "requires_clarify": bool(coded_filter_count and not matches),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: schema_sample
# ═══════════════════════════════════════════════════════════════════════════════

def schema_sample(args: dict[str, Any], task_id: str | None = None) -> str:
    table_raw = str(args.get("table") or "").strip()
    if not table_raw:
        return json.dumps({"success": False, "error": "`table` is required."}, ensure_ascii=False)
    try:
        table = _safe_ident(table_raw, kind="table")
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    cols_raw = args.get("columns")
    if cols_raw and isinstance(cols_raw, list):
        try:
            cols = [_safe_ident(str(c), kind="column") for c in cols_raw if str(c).strip()]
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
    else:
        cols = _columns_for(table)
        if not cols:
            return json.dumps(
                {"success": False, "error": f"Table `{table}` not found or has no columns."},
                ensure_ascii=False,
            )

    row_examples: list[dict[str, Any]] = []
    try:
        profile = bool(cols_raw and isinstance(cols_raw, list))
        probe_cols = cols[: min(len(cols), _PROBE_FIELD_LIMIT)]
        result = _column_formats(table, cols[:20], profile=profile)
        row_examples = _sample_row_examples(table, probe_cols)
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
            default=str,
        )

    return json.dumps(
        {"success": True, "table": table, "columns": result, "row_examples": row_examples},
        ensure_ascii=False,
        default=str,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: count_rows
# ═══════════════════════════════════════════════════════════════════════════════

def count_rows(args: dict[str, Any], task_id: str | None = None) -> str:
    specs = args.get("tables")
    if not isinstance(specs, list) or not specs:
        return json.dumps(
            {"success": False, "error": "`tables` must be a non-empty list."},
            ensure_ascii=False,
        )
    table_intent_confirmed = bool(args.get("table_intent_confirmed"))
    table_hints = _table_selection_hints(specs)
    if table_hints.get("requires_clarify") and not table_intent_confirmed:
        blocked_payload: dict[str, Any] = {
            "success": False,
            "blocked": True,
            "hard_stop": True,
            "retryable": False,
            "requires_clarify": True,
            "next_action": "clarify",
            "error": (
                "Table intent is ambiguous: related candidate tables exist in dictionary. "
                "Ask clarify first, then rerun count_rows with table_intent_confirmed=true."
            ),
            "table_selection_hints": table_hints,
        }
        return json.dumps(blocked_payload, ensure_ascii=False, default=str)

    try:
        per_table = [_count_one_table(spec) for spec in specs]
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
            default=str,
        )

    total = sum(int(t["count"]) for t in per_table)
    payload: dict[str, Any] = {
        "success": True,
        "number": total,
        "final_sql": _combined_final_sql(per_table),
        "tables": [
            {
                "table": t["table"],
                "count": t["count"],
                "table_total_rows": t.get("table_total_rows"),
                "sql": t["count_sql"],
                "coverage": t.get("coverage"),
            }
            for t in per_table
        ],
    }
    if table_hints["selected_tables"] or table_hints["alternative_candidates"]:
        payload["table_selection_hints"] = table_hints
        if table_hints.get("requires_clarify"):
            payload["evidence_warning"] = (
                "Alternative related tables exist in dictionary. Confirmed intent supplied; "
                "make sure final answer states why this table was chosen."
            )
    filter_evidence = _dictionary_filter_evidence(specs)
    if (
        filter_evidence["dictionary_matches"]
        or filter_evidence["dictionary_mismatches"]
        or filter_evidence["unverified_coded_filters"]
    ):
        payload["filter_evidence"] = filter_evidence
        if filter_evidence["dictionary_mismatches"] or filter_evidence["unverified_coded_filters"]:
            payload["evidence_warning"] = (
                "Some coded filters are not verified by dictionary codebook evidence. "
                "Do not assert label semantics for those filters without clarify/read_file follow-up."
            )
        if filter_evidence.get("requires_clarify"):
            payload["requires_clarify"] = True
    if total == 0:
        diag = _zero_count_diagnostic(specs)
        if diag:
            payload["zero_count_diagnostic"] = diag
    return json.dumps(payload, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: database_query
# ═══════════════════════════════════════════════════════════════════════════════

def database_query(args: dict[str, Any], task_id: str | None = None) -> str:
    sql = args.get("sql")
    n = args.get("max_rows")
    if isinstance(n, str) and n.strip().isdigit():
        n = int(n.strip())
    if not isinstance(n, int) or n <= 0:
        n = 200

    s = str(sql or "").strip()
    if not s:
        return _fail(task_id, {"error": "Empty SQL.", "sql": ""})
    core = s.rstrip(";").strip()

    if ";" in core:
        return _fail(task_id, {"error": "Multiple SQL statements not allowed.", "sql": core})

    head = core.lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        return _fail(task_id, {"error": "Only SELECT or WITH … SELECT are allowed.", "sql": core})
    if _DENY_RW.search(core):
        return _fail(task_id, {"error": "Disallowed keyword for read-only mode.", "sql": core})
    if re.search(r"\bINTO\b", core, re.IGNORECASE):
        return _fail(task_id, {"error": "INTO clauses are not allowed.", "sql": core})

    key = task_id or "_global"

    try:
        rows, returns_rows, cut = _run_select(core, n)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.warning("database_query failed: %s", msg)
        payload: dict[str, Any] = {"error": msg, "sql": core}
        # On SQL error: suggest available columns or tables
        if _RE_INVALID_COL.search(msg):
            tm = _RE_FROM_TABLE.search(core)
            if tm:
                cols = _columns_for(tm.group(1))
                if cols:
                    payload["evidence"] = {"available_columns": {tm.group(1): cols}}
        elif "no such table" in msg.lower():
            tm = _RE_FROM_TABLE.search(core)
            if tm:
                similar = _similar_tables(tm.group(1), _list_tables())
                if similar:
                    payload["evidence"] = {"candidate_tables": similar}
        return _fail(task_id, payload)

    _FAIL_COUNTS.pop(key, None)
    payload = {
        "success": True,
        "sql": core,
        "rows": rows,
        "returns_rows": bool(returns_rows),
        "truncated": bool(cut),
        "row_count": len(rows),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA_SAMPLE_SCHEMA = {
    "name": "schema_sample",
    "description": (
        "Inspect how a table stores data: column types, distinct samples, row_examples "
        "(non-null rows), and value_distribution when `columns` is set. "
        "Use before filtering on unverified qualifiers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table name."},
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key columns to inspect (recommended). Omit to sample all (capped at 20).",
            },
        },
        "required": ["table"],
        "additionalProperties": False,
    },
}

COUNT_ROWS_SCHEMA = {
    "name": "count_rows",
    "description": (
        "COUNT rows with structured filters and optional date range. "
        "Prefer over database_query for simple counts. "
        "On 0, returns zero_count_diagnostic with sample values on filter columns. "
            "When related candidate tables exist, the call is blocked "
        "unless `table_intent_confirmed=true`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "description": "One entry per table; results are summed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "filters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "column": {"type": "string"},
                                    "op": {"type": "string", "description": "=, !=, <>, >, >=, <, <=, IN, NOT IN"},
                                    "value": {},
                                },
                                "required": ["column", "op", "value"],
                            },
                        },
                        "date_column": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["table"],
                },
            },
            "table_intent_confirmed": {
                "type": "boolean",
                "description": "Set true only after user intent confirms the selected table when alternatives exist.",
            },
        },
        "required": ["tables"],
        "additionalProperties": False,
    },
}

DATABASE_QUERY_SCHEMA = {
    "name": "database_query",
    "description": (
        "Run one read-only SELECT (or WITH … SELECT). "
        "Use for analysis SQL (GROUP BY, JOIN, trends); prefer count_rows for counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "One SELECT or WITH … SELECT."},
            "max_rows": {"type": "integer", "description": "Row cap (default 200)."},
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}

_DIALECT_HINTS = {
    "mssql": "mssql: use TOP N (not LIMIT); prefix Unicode literals with N'...'.",
    "pg": "postgresql: use LIMIT N.",
    "mysql": "mysql: use LIMIT N.",
    "sqlite": "sqlite: use LIMIT N.",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════

registry.register(
    name="schema_sample",
    toolset="database",
    schema=SCHEMA_SAMPLE_SCHEMA,
    handler=lambda args, **kw: schema_sample(args, task_id=kw.get("task_id")),
    check_fn=configured,
    requires_env=[],
    emoji="🔍",
    max_result_size_chars=40_000,
)

registry.register(
    name="count_rows",
    toolset="database",
    schema=COUNT_ROWS_SCHEMA,
    handler=lambda args, **kw: count_rows(args, task_id=kw.get("task_id")),
    check_fn=configured,
    requires_env=[],
    emoji="🔢",
    max_result_size_chars=40_000,
)

registry.register(
    name="database_query",
    toolset="database",
    schema=DATABASE_QUERY_SCHEMA,
    handler=lambda args, **kw: database_query(args, task_id=kw.get("task_id")),
    check_fn=configured,
    requires_env=[],
    dynamic_schema_overrides=lambda: {"description": DATABASE_QUERY_SCHEMA["description"] + " " + _DIALECT_HINTS.get(_dialect_kind(), "Verify LIMIT-vs-TOP and date formatting for your dialect.")},
    emoji="🗄️",
    max_result_size_chars=120_000,
)
