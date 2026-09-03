"""Estatísticas de indexação + efetividade de uso, calculadas a partir do
grafo de um projeto -- usado pela rota /dashboard do proxy (proxy.py).
"""

import json
import statistics
import sqlite3


def _is_smart_chunk(name: str) -> bool:
    """Um chunk do fallback burro de linha tem nome tipo "linhas 1-150".
    Qualquer outro nome veio de chunking de verdade (tree-sitter/markdown)."""
    return not name.startswith("linhas ")


def coverage_stats(conn: sqlite3.Connection) -> dict:
    files = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='file'").fetchone()[0]
    contexts = conn.execute("SELECT name FROM nodes WHERE type='file_context'").fetchall()
    total = len(contexts)
    smart = sum(1 for c in contexts if _is_smart_chunk(c["name"]))
    flows = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='flow'").fetchone()[0]
    flow_steps = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='flow_step'").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return {
        "files": files,
        "contexts_total": total,
        "contexts_smart": smart,
        "contexts_smart_pct": round(100 * smart / total, 1) if total else 0.0,
        "flows": flows,
        "flow_steps": flow_steps,
        "flow_edges_resolved": edges,
    }


def avg_chunk_size(conn: sqlite3.Connection) -> float:
    """Tamanho médio (caracteres) dos nós file_context -- o que uma tool
    tipo get_file_tree/get_node/get_flow efetivamente devolve."""
    rows = conn.execute(
        "SELECT LENGTH(content) AS n FROM nodes WHERE type='file_context' AND content IS NOT NULL"
    ).fetchall()
    sizes = [r["n"] for r in rows if r["n"]]
    return statistics.mean(sizes) if sizes else 0.0


def avg_file_size(conn: sqlite3.Connection) -> float:
    """Tamanho médio (bytes) do arquivo inteiro -- o que o modelo teria
    que ler sem o codegraph-mcp."""
    rows = conn.execute(
        "SELECT json_extract(metadata, '$.size_bytes') AS n FROM nodes WHERE type='file'"
    ).fetchall()
    sizes = [r["n"] for r in rows if r["n"]]
    return statistics.mean(sizes) if sizes else 0.0


def history_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, metadata FROM nodes WHERE type='history' ORDER BY created_at ASC"
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["metadata"]) if r["metadata"] else {}
        out.append({"id": r["id"], "created_at": r["created_at"], **meta})
    return out


def effectiveness_summary(conn: sqlite3.Connection) -> dict:
    hist = history_rows(conn)
    n = len(hist)
    empty = {
        "exchanges": 0, "with_tools": 0, "with_tools_pct": 0.0,
        "avg_prompt_tokens": None, "avg_completion_tokens": None,
        "avg_tokens_per_sec": None, "estimated_tokens_saved": 0,
    }
    if n == 0:
        return empty

    with_tools = [h for h in hist if h.get("used_codegraph_tools")]
    prompt_tokens = [h["prompt_tokens"] for h in hist if h.get("prompt_tokens") is not None]
    completion_tokens = [h["completion_tokens"] for h in hist if h.get("completion_tokens") is not None]
    speeds = [h["predicted_per_second"] for h in hist if h.get("predicted_per_second") is not None]

    # Estimativa (não medição exata): cada troca que usou uma tool do
    # codegraph-mcp poupou aproximadamente a diferença entre o tamanho
    # médio de um arquivo inteiro e o tamanho médio de um chunk devolvido,
    # convertido pra tokens numa razão grosseira de ~4 chars/token.
    avg_chunk = avg_chunk_size(conn)
    avg_file = avg_file_size(conn)
    saved_chars = max(avg_file - avg_chunk, 0) * len(with_tools)
    estimated_tokens_saved = round(saved_chars / 4)

    return {
        "exchanges": n,
        "with_tools": len(with_tools),
        "with_tools_pct": round(100 * len(with_tools) / n, 1),
        "avg_prompt_tokens": round(statistics.mean(prompt_tokens)) if prompt_tokens else None,
        "avg_completion_tokens": round(statistics.mean(completion_tokens)) if completion_tokens else None,
        "avg_tokens_per_sec": round(statistics.mean(speeds), 1) if speeds else None,
        "estimated_tokens_saved": estimated_tokens_saved,
    }
