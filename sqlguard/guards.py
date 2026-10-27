"""Layer 1: deterministic guards around SQL execution.

Nothing here is a model. Each guard is a plain check whose verdict can be
reproduced by hand, and each can be switched off on its own for the
leave-one-out ablation (``GuardConfig``):

* ``allowlist``     static check of the SQL text: one statement, starts with
                    SELECT or WITH, no RECURSIVE, no PRAGMA/ATTACH/write
                    keywords outside string literals.
* ``readonly``      the database is opened with ``mode=ro`` and
                    ``PRAGMA query_only = ON``; with it off the query runs on a
                    writable in-memory copy (never on the source file).
* ``authz_actions`` sqlite3 authorizer: only SELECT, READ and allowlisted
                    functions; every write, DDL, PRAGMA, ATTACH, transaction
                    or recursive CTE is denied at prepare time.
* ``authz_columns`` the table/column policy of ``schema.py``, also enforced
                    in the authorizer, where sqlite reports every column a
                    statement reads (SELECT list, WHERE, JOIN, aggregates).
* ``force_limit``   the query is wrapped as ``SELECT * FROM (...) LIMIT n+1``
                    and at most n rows are fetched; more is reported as
                    truncated.
* ``timeout``       a progress handler interrupts the statement after
                    ``timeout_s`` of wall time.

With ``force_limit`` off the harness still stops fetching at ``harness_rows``
(and marks the result truncated), and with ``timeout`` off it still stops a
statement after ``harness_wall_s`` (status ``runaway``), only so that an
ablation cannot exhaust the machine running it. Neither is a guard: the
oracle reports such rows as unbounded and a runaway as an unbounded execution.

A query that fails a guard is refused with the guard's name as the reason.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .schema import READABLE_TABLES, is_restricted

ALLOWED_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "total", "round", "abs", "coalesce", "ifnull", "nullif", "iif",
    "strftime", "date", "datetime", "julianday", "time", "unixepoch", "substr", "substring", "lower", "upper",
    "length", "trim", "ltrim", "rtrim", "replace", "instr", "printf", "format", "group_concat", "string_agg",
    "like", "glob", "cast", "typeof", "char", "random", "row_number", "rank", "dense_rank", "lag", "lead",
    "ntile", "first_value", "last_value", "nth_value", "percent_rank", "cume_dist", "sign", "max", "min",
})
FORBIDDEN_WORDS = ("insert", "update", "delete", "replace", "drop", "create", "alter", "attach", "detach",
                   "pragma", "vacuum", "reindex", "analyze", "begin", "commit", "rollback", "savepoint",
                   "release", "recursive", "load_extension")


@dataclass
class GuardConfig:
    allowlist: bool = True
    readonly: bool = True
    authz_actions: bool = True
    authz_columns: bool = True
    force_limit: bool = True
    timeout: bool = True
    row_limit: int = 1000
    timeout_s: float = 2.0
    progress_every: int = 1000
    harness_rows: int = 10001
    harness_wall_s: float = 5.0

    def without(self, name: str) -> "GuardConfig":
        d = asdict(self)
        d[name] = False
        return GuardConfig(**d)


GUARD_NAMES = ("allowlist", "readonly", "authz_actions", "authz_columns", "force_limit", "timeout")


@dataclass
class GuardResult:
    status: str                      # "ok" | "blocked" | "error" | "runaway" (stopped by the harness, not a guard)
    guard: str | None = None         # which guard blocked it (status "blocked")
    reason: str = ""
    sql_run: str | None = None       # the statement actually executed (after wrapping)
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    elapsed_s: float = 0.0
    denied: list[str] = field(default_factory=list)  # authorizer denials, "action:table.column"

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ static allowlist


def strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments outside string literals and quoted identifiers."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`", "["):
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:  # doubled quote escape
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def code_only(sql: str) -> str:
    """The SQL with comments removed and string literals blanked, for keyword checks."""
    s = strip_comments(sql)
    return re.sub(r"'(?:[^']|'')*'", "''", s)


def normalize(sql: str) -> str:
    """Comments removed, surrounding whitespace and trailing semicolons dropped."""
    s = strip_comments(sql).strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def allowlist_check(sql: str) -> str | None:
    """None if the statement passes, else the reason it is refused."""
    s = normalize(sql)
    if not s:
        return "empty statement"
    code = code_only(s)
    if ";" in code:
        return "more than one statement"
    first = re.match(r"\s*([A-Za-z]+)", code)
    if not first or first.group(1).lower() not in ("select", "with"):
        return f"statement type not allowed: {first.group(1).upper() if first else '?'}"
    words = set(re.findall(r"[a-z_]+", code.lower()))
    bad = [w for w in FORBIDDEN_WORDS if w in words]
    # REPLACE is also a string function; allow replace( ... ) calls
    if "replace" in bad and not re.search(r"\breplace\s+into\b", code, re.I):
        bad.remove("replace")
    if bad:
        return f"forbidden keyword: {bad[0].upper()}"
    return None


def wrap_limit(sql: str, n: int) -> str:
    return f"SELECT * FROM (\n{normalize(sql)}\n) LIMIT {int(n) + 1}"


# ------------------------------------------------------------------ authorizer


_ACTION_NAMES = {getattr(sqlite3, a): a[len("SQLITE_"):].lower() for a in dir(sqlite3)
                 if a.startswith("SQLITE_") and isinstance(getattr(sqlite3, a), int)
                 and a not in ("SQLITE_OK", "SQLITE_DENY", "SQLITE_IGNORE") and not a.startswith("SQLITE_LIMIT")
                 and not a.startswith("SQLITE_PREPARE")}


def make_authorizer(cfg: GuardConfig, denied: list[str]):
    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table, col = (arg1 or "").lower(), (arg2 or "").lower()
            if cfg.authz_columns and (table not in READABLE_TABLES or is_restricted(table, col)):
                denied.append(f"read:{table}.{col}")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            fn = (arg2 or "").lower()
            if cfg.authz_actions and fn not in ALLOWED_FUNCTIONS:
                denied.append(f"function:{fn}")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if cfg.authz_actions:
            denied.append(f"{_ACTION_NAMES.get(action, action)}:{arg1 or ''}.{arg2 or ''}")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return auth


# ------------------------------------------------------------------ execution


def open_connection(db_path: str | Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, check_same_thread=False)
        con.execute("PRAGMA query_only = ON")
        return con
    src = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    src.backup(con)
    src.close()
    return con


def run_guarded(db_path: str | Path, sql: str, cfg: GuardConfig | None = None) -> GuardResult:
    cfg = cfg or GuardConfig()
    if cfg.allowlist:
        why = allowlist_check(sql)
        if why:
            return GuardResult("blocked", "allowlist", why)
    run = wrap_limit(sql, cfg.row_limit) if cfg.force_limit else normalize(sql)
    con = open_connection(db_path, cfg.readonly)
    denied: list[str] = []
    con.set_authorizer(make_authorizer(cfg, denied))
    t0 = time.perf_counter()
    deadline = t0 + (cfg.timeout_s if cfg.timeout else cfg.harness_wall_s)
    con.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, cfg.progress_every)
    try:
        cur = con.execute(run)
        cols = [d[0] for d in cur.description or []]
        shown = cfg.row_limit if cfg.force_limit else cfg.harness_rows - 1
        rows = cur.fetchmany(shown + 1)
        truncated = len(rows) > shown
        rows = rows[:shown]
        return GuardResult("ok", sql_run=run, columns=cols, rows=[list(r) for r in rows], truncated=truncated,
                           elapsed_s=time.perf_counter() - t0, denied=denied)
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        elapsed = time.perf_counter() - t0
        if denied or "not authorized" in msg:
            g = "authz_columns" if any(d.startswith("read:") for d in denied) else "authz_actions"
            return GuardResult("blocked", g, f"{msg}: {', '.join(denied[:3])}", sql_run=run, elapsed_s=elapsed,
                               denied=denied)
        if "interrupted" in msg and not cfg.timeout:
            return GuardResult("runaway", None, f"stopped by the harness after {cfg.harness_wall_s:.1f} s",
                               sql_run=run, elapsed_s=elapsed)
        if "interrupted" in msg:
            return GuardResult("blocked", "timeout", f"interrupted after {cfg.timeout_s:.1f} s", sql_run=run,
                               elapsed_s=elapsed)
        if "readonly" in msg.replace("-", "").lower() or "read-only" in msg.lower():
            return GuardResult("blocked", "readonly", msg, sql_run=run, elapsed_s=elapsed)
        return GuardResult("error", None, msg, sql_run=run, elapsed_s=elapsed)
    except (sqlite3.Warning, sqlite3.ProgrammingError) as exc:  # e.g. "You can only execute one statement at a time"
        return GuardResult("error", None, str(exc), sql_run=run, elapsed_s=time.perf_counter() - t0)
    finally:
        con.close()
