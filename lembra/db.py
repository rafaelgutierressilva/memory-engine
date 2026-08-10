"""Storage layer: connection, schema bootstrap, and the primitive reads/writes.

No intelligence lives here — the gate (Fase 2), sleep (Fase 3) and retrieval
(Fase 4) all sit on top of these primitives.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

EPISODE_STATUSES = ("buffered", "consolidated", "expired", "deleted")
FACT_STATUSES = ("active", "sunken", "retired")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_days(value: str | None, ref: datetime | None = None) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    ref = ref or datetime.now(timezone.utc)
    return (ref - dt).total_seconds() / 86400.0


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.load().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    ensure_fts(conn)


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Constroi o indice FTS uma vez, para o que existia antes dos gatilhos.

    Usa um marcador em `meta` em vez de tentar detectar indice vazio: numa
    tabela FTS de conteudo externo, `COUNT(*)` conta a tabela de ORIGEM, nao o
    indice — ou seja, o indice pode estar vazio e o contador dizer que nao.
    """
    if conn.execute("SELECT 1 FROM meta WHERE key = 'fts_built'").fetchone():
        return
    rebuild_fts(conn)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    for fts in ("fts_facts", "fts_episodes"):
        conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_built', ?)",
                 (now_iso(),))
    conn.commit()


def open_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn


# ---------------------------------------------------------------- events

def log_event(conn, kind: str, *, episode_id: int | None = None,
              fact_id: int | None = None, **payload) -> int:
    cur = conn.execute(
        "INSERT INTO events (timestamp, kind, episode_id, fact_id, payload) "
        "VALUES (?,?,?,?,?)",
        (now_iso(), kind, episode_id, fact_id, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


def list_events(conn, kind: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    if kind:
        return conn.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# -------------------------------------------------------------- episodes

def add_episode(conn, raw_text: str, *, surprise_score: float = 0.0,
                reasons: list[str] | None = None, status: str = "buffered",
                timestamp: str | None = None) -> int:
    if status not in EPISODE_STATUSES:
        raise ValueError(f"status invalido: {status}")
    ts = timestamp or now_iso()
    cur = conn.execute(
        "INSERT INTO episodes (timestamp, raw_text, surprise_score, surprise_reasons, "
        "status, status_changed_at) VALUES (?,?,?,?,?,?)",
        (ts, raw_text, float(surprise_score),
         json.dumps(reasons or [], ensure_ascii=False), status, ts),
    )
    conn.commit()
    return cur.lastrowid


def get_episode(conn, episode_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()


def list_episodes(conn, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM episodes WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def set_episode_status(conn, episode_id: int, status: str) -> None:
    if status not in EPISODE_STATUSES:
        raise ValueError(f"status invalido: {status}")
    conn.execute(
        "UPDATE episodes SET status = ?, status_changed_at = ? WHERE id = ?",
        (status, now_iso(), episode_id),
    )
    conn.commit()


def touch_episode(conn, episode_id: int) -> None:
    """Recuperar alimenta o decaimento: todo uso conta."""
    conn.execute(
        "UPDATE episodes SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
        (now_iso(), episode_id),
    )
    conn.commit()


# ----------------------------------------------------------------- facts

def add_fact(conn, statement: str, *, valid_from: str | None = None,
             confidence: float = 0.5, is_core: bool = False,
             episode_ids: list[int] | None = None,
             supersedes: int | None = None,
             status: str = "active") -> int:
    if status not in FACT_STATUSES:
        raise ValueError(f"status invalido: {status}")
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO facts (statement, valid_from, valid_to, confidence, created_at, "
        "last_confirmed_at, status, is_core, supersedes) VALUES (?,?,NULL,?,?,?,?,?,?)",
        (statement, valid_from or ts, float(confidence), ts, ts, status,
         1 if is_core else 0, supersedes),
    )
    fact_id = cur.lastrowid
    for eid in episode_ids or []:
        link_provenance(conn, fact_id, eid, commit=False)
    if supersedes is not None:
        conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (fact_id, supersedes))
    conn.commit()
    return fact_id


def get_fact(conn, fact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()


def list_facts(conn, status: str | None = None, current_only: bool = False,
               limit: int = 100) -> list[sqlite3.Row]:
    sql = "SELECT * FROM facts"
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if current_only:
        clauses.append("valid_to IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def set_fact_status(conn, fact_id: int, status: str) -> None:
    if status not in FACT_STATUSES:
        raise ValueError(f"status invalido: {status}")
    conn.execute("UPDATE facts SET status = ? WHERE id = ?", (status, fact_id))
    conn.commit()


def close_fact(conn, fact_id: int, valid_to: str | None = None) -> None:
    """Encerra a vigencia — nunca sobrescreve. A historia fica consultavel."""
    conn.execute("UPDATE facts SET valid_to = ? WHERE id = ?",
                 (valid_to or now_iso(), fact_id))
    conn.commit()


def touch_fact(conn, fact_id: int) -> None:
    conn.execute(
        "UPDATE facts SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
        (now_iso(), fact_id),
    )
    conn.commit()


def bump_confidence(conn, fact_id: int, step: float, ceiling: float) -> tuple[float, float]:
    row = get_fact(conn, fact_id)
    if row is None:
        raise KeyError(f"fato #{fact_id} nao existe")
    before = row["confidence"]
    after = min(ceiling, before + step)
    conn.execute(
        "UPDATE facts SET confidence = ?, last_confirmed_at = ? WHERE id = ?",
        (after, now_iso(), fact_id),
    )
    conn.commit()
    return before, after


# ------------------------------------------------------------ provenance

def link_provenance(conn, fact_id: int, episode_id: int, commit: bool = True) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO provenance (fact_id, episode_id) VALUES (?,?)",
        (fact_id, episode_id),
    )
    if commit:
        conn.commit()


def episodes_for_fact(conn, fact_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.* FROM episodes e JOIN provenance p ON p.episode_id = e.id "
        "WHERE p.fact_id = ? ORDER BY e.timestamp",
        (fact_id,),
    ).fetchall()


def facts_for_episode(conn, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.* FROM facts f JOIN provenance p ON p.fact_id = f.id "
        "WHERE p.episode_id = ? ORDER BY f.id",
        (episode_id,),
    ).fetchall()


def orphan_facts(conn) -> list[sqlite3.Row]:
    """Fatos sem proveniencia — nunca deveriam existir fora do seed manual."""
    return conn.execute(
        "SELECT f.* FROM facts f LEFT JOIN provenance p ON p.fact_id = f.id "
        "WHERE p.fact_id IS NULL ORDER BY f.id"
    ).fetchall()


# ----------------------------------------------------------------- stats

def stats(conn) -> dict:
    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    ep_by_status = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM episodes GROUP BY status")
    }
    fa_by_status = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM facts GROUP BY status")
    }
    ev_by_kind = {
        r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC")
    }
    return {
        "episodes_total": count("SELECT COUNT(*) FROM episodes"),
        "episodes_by_status": ep_by_status,
        "facts_total": count("SELECT COUNT(*) FROM facts"),
        "facts_by_status": fa_by_status,
        "facts_current": count("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL"),
        "facts_closed": count("SELECT COUNT(*) FROM facts WHERE valid_to IS NOT NULL"),
        "facts_core": count("SELECT COUNT(*) FROM facts WHERE is_core = 1"),
        "provenance_links": count("SELECT COUNT(*) FROM provenance"),
        "facts_without_provenance": len(orphan_facts(conn)),
        "events_total": count("SELECT COUNT(*) FROM events"),
        "events_by_kind": ev_by_kind,
        "avg_surprise_stamped": conn.execute(
            "SELECT AVG(surprise_score) FROM episodes WHERE surprise_score > 0"
        ).fetchone()[0],
    }
