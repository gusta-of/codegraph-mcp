"""Histórico de prompt<->resposta como nós `history` no grafo, com teto de
tamanho configurável -- só esse tipo de nó é expurgado (mais antigo
primeiro) quando o banco passa do limite. Indexação de projeto
(file/file_context/flow/flow_step) nunca é tocada por esse mecanismo.
"""

import json
import os
import sqlite3
from pathlib import Path

from codegraph import db

DEFAULT_MAX_HISTORY_MB = 15 * 1024  # 15 GiB
CONFIG_FILENAME = "codegraph-history.json"


def config_path(project_root: Path) -> Path:
    return project_root / ".kimi-code" / CONFIG_FILENAME


def load_max_bytes(project_root: Path) -> int:
    path = config_path(project_root)
    if not path.exists():
        return DEFAULT_MAX_HISTORY_MB * 1024 * 1024
    data = json.loads(path.read_text())
    return int(data.get("max_history_mb", DEFAULT_MAX_HISTORY_MB)) * 1024 * 1024


def write_default_config(project_root: Path) -> Path:
    """Cria o arquivo de config se ainda não existir (não sobrescreve
    ajuste manual do usuário). Chamado pelo setup-project.sh."""
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps({"max_history_mb": DEFAULT_MAX_HISTORY_MB}, indent=2) + "\n")
    return path


class LimitTooSmallError(ValueError):
    """Usuário tentou definir um teto menor que o banco já ocupa hoje."""

    def __init__(self, requested_mb: float, current_mb: float):
        self.requested_mb = requested_mb
        self.current_mb = current_mb
        super().__init__(
            f"o banco já ocupa {current_mb:.1f} MB -- não dá pra definir um teto menor "
            f"({requested_mb} MB) sem apagar dados. Se você quer mesmo um teto menor, "
            f"apague o arquivo .codegraph/graph.db manualmente e rode `codegraph setup` "
            f"de novo pra reindexar do zero já com o teto novo."
        )


def set_max_mb(project_root: Path, mb: int, db_path: Path) -> None:
    """Grava um novo teto -- recusa (`LimitTooSmallError`) se `mb` for
    menor que o tamanho atual do arquivo .db. Sem essa checagem,
    `enforce_limit` entraria num estado impossível de satisfazer sem
    apagar tudo, incluindo indexação (que ele nunca apaga -- ver seção
    9.4 do ARQUITETURA.md), silenciosamente."""
    if db_path.exists():
        current_mb = db_path.stat().st_size / (1024 * 1024)
        if mb < current_mb:
            raise LimitTooSmallError(requested_mb=mb, current_mb=current_mb)

    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        data = json.loads(path.read_text())
    data["max_history_mb"] = mb
    path.write_text(json.dumps(data, indent=2) + "\n")


def record_exchange(
    conn: sqlite3.Connection,
    db_path: str,
    *,
    prompt: str,
    response: str,
    max_bytes: int,
    metadata: dict | None = None,
) -> int:
    """Grava um nó `history` (prompt+resposta) e expurga entradas antigas
    se o banco passou do limite configurado. Devolve o id do nó criado."""
    name = (prompt or "").strip().replace("\n", " ")[:80]
    content = f"PROMPT:\n{prompt}\n\nRESPONSE:\n{response}"
    node_id = db.upsert_node(
        conn, type="history", name=name or "(prompt vazio)",
        content=content, metadata=metadata or {},
    )
    conn.commit()
    enforce_limit(conn, db_path, max_bytes)
    return node_id


def enforce_limit(conn: sqlite3.Connection, db_path: str, max_bytes: int) -> int:
    """Apaga nós `history` mais antigos (created_at ASC) até o arquivo .db
    caber no limite, ou até não sobrar mais nenhum `history` pra apagar
    (indexação nunca é removida, mesmo que sozinha já exceda o limite).
    Devolve quantas entradas foram removidas."""
    removed = 0
    while os.path.getsize(db_path) > max_bytes:
        row = conn.execute(
            "SELECT id FROM nodes WHERE type='history' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            break
        conn.execute("DELETE FROM nodes WHERE id=?", (row["id"],))
        conn.commit()
        removed += 1
    return removed
