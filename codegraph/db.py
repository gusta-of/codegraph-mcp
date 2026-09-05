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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bancos criados antes do tipo `history` existir têm o CHECK antigo
    (só file/file_context/flow/flow_step) fisicamente gravado -- reescrever
    `CREATE TABLE IF NOT EXISTS` não atualiza um CHECK já existente.
    Detecta isso e reconstrói a tabela `nodes` com o schema atual.

    Aproveita pra ligar `auto_vacuum=FULL` (via VACUUM) -- sem isso, DELETE
    não libera espaço em disco de verdade, e o expurgo por tamanho
    (codegraph/history.py) mede o tamanho real do arquivo .db.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes'"
    ).fetchone()
    needs_type_migration = row is not None and "'history'" not in row["sql"]

    auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    needs_vacuum_migration = auto_vacuum != 1  # 1 = FULL

    if not needs_type_migration and not needs_vacuum_migration:
        return

    if needs_type_migration:
        conn.executescript(
            """
            PRAGMA foreign_keys=off;
            ALTER TABLE nodes RENAME TO nodes_old;
            """
        )
        # recria só a tabela `nodes` (schema.sql tem mais coisa -- edges/fts/
        # triggers já existem e não mudaram, IF NOT EXISTS não mexe neles)
        conn.execute(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK (type IN ('file', 'file_context', 'flow', 'flow_step', 'history')),
                parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                path TEXT,
                content TEXT,
                start_line INTEGER,
                end_line INTEGER,
                content_hash TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute("INSERT INTO nodes SELECT * FROM nodes_old")
        conn.execute("DROP TABLE nodes_old")
        conn.execute("PRAGMA foreign_keys=on")
        conn.commit()
        # o índice do FTS aponta pros rowids antigos -- reconstrói do zero
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
        conn.commit()

    if needs_vacuum_migration:
        conn.commit()
        conn.execute("PRAGMA auto_vacuum = FULL")
        conn.execute("VACUUM")


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


def get_recent(
    conn: sqlite3.Connection, type: str, limit: int = 20, before_id: int | None = None,
) -> list[sqlite3.Row]:
    """Página de nós do tipo dado, mais recentes primeiro. Ordena por `id`
    (não `created_at`): id é autoincrement, já reflete a ordem cronológica
    real de criação e não tem empate entre linhas gravadas no mesmo
    segundo -- necessário pra paginação por cursor (`before_id`) ser
    estável mesmo com registros novos chegando entre uma página e outra
    (achado real, 2026-09-05, ver ARQUITETURA.md)."""
    if before_id is not None:
        return conn.execute(
            "SELECT * FROM nodes WHERE type=? AND id<? ORDER BY id DESC LIMIT ?",
            (type, before_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM nodes WHERE type=? ORDER BY id DESC LIMIT ?", (type, limit)
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


def _sanitize_fts_query(query: str) -> str:
    """Envolve cada palavra em aspas duplas (escapando aspas internas no
    jeito do FTS5: `"` vira `""`), transformando a busca numa sequência de
    frases literais em AND implícito. Sem isso, qualquer termo com pontuação
    fora do padrão bareword do parser do FTS5 -- nome de arquivo com ponto
    (`audio.ts`), hífen, dois-pontos -- derruba a query com
    `fts5: syntax error`, já que o parser da MATCH tenta interpretar esses
    caracteres como operador especial em vez de parte do termo (achado real,
    2026-09-05: foi provavelmente por isso que o agente viu "busca vazia" e
    caiu pro Grep -- a tool não devolvia vazio, ela quebrava. Ver
    ARQUITETURA.md). Isso troca a sintaxe avançada do FTS5 (ex: "auth AND
    token" como booleano) por match literal -- aceitável aqui porque quem
    chama essa tool é o agente, não um usuário digitando query FTS5 de
    propósito."""
    terms = query.split()
    if not terms:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT nodes.* FROM nodes_fts
           JOIN nodes ON nodes.id = nodes_fts.rowid
           WHERE nodes_fts MATCH ?
           ORDER BY rank LIMIT ?""",
        (_sanitize_fts_query(query), limit),
    ).fetchall()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("metadata"):
        d["metadata"] = json.loads(d["metadata"])
    return d
