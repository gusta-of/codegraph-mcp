"""Camada fina sobre SQLite pra guardar o grafo (nós + arestas)."""

import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def upsert_node(
    conn: sqlite3.Connection,
    *,
    type: str,
    name: str,
    parent_id: int | None = None,
    path: str | None = None,
    content: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    content_hash: str | None = None,
    metadata: dict | None = None,
    existing_id: int | None = None,
) -> int:
    """Cria um nó novo, ou atualiza um já existente (se existing_id for dado)."""
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    if existing_id is not None:
        conn.execute(
            """UPDATE nodes SET name=?, path=?, content=?, start_line=?, end_line=?,
                   content_hash=?, metadata=?, updated_at=datetime('now')
               WHERE id=?""",
            (name, path, content, start_line, end_line, content_hash, meta_json, existing_id),
        )
        return existing_id
    cur = conn.execute(
        """INSERT INTO nodes (type, parent_id, name, path, content, start_line, end_line,
                               content_hash, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (type, parent_id, name, path, content, start_line, end_line, content_hash, meta_json),
    )
    return cur.lastrowid


def find_node(conn: sqlite3.Connection, *, type: str, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM nodes WHERE type=? AND path=?", (type, path)
    ).fetchone()


def delete_children(conn: sqlite3.Connection, parent_id: int) -> None:
    conn.execute("DELETE FROM nodes WHERE parent_id=?", (parent_id,))


def get_node(conn: sqlite3.Connection, node_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()


def get_children(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE parent_id=? ORDER BY start_line, id", (node_id,)
    ).fetchall()


def get_roots(conn: sqlite3.Connection, type: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE type=? AND parent_id IS NULL ORDER BY name", (type,)
    ).fetchall()


def add_edge(conn: sqlite3.Connection, src_id: int, dst_id: int, type: str, metadata: dict | None = None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO edges (src_id, dst_id, type, metadata)
           VALUES (?, ?, ?, ?)""",
        (src_id, dst_id, type, json.dumps(metadata or {}, ensure_ascii=False)),
    )


def get_edges_from(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM edges WHERE src_id=?", (node_id,)).fetchall()


def get_edges_to(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM edges WHERE dst_id=?", (node_id,)).fetchall()


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT nodes.* FROM nodes_fts
           JOIN nodes ON nodes.id = nodes_fts.rowid
           WHERE nodes_fts MATCH ?
           ORDER BY rank LIMIT ?""",
        (query, limit),
    ).fetchall()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("metadata"):
        d["metadata"] = json.loads(d["metadata"])
    return d
