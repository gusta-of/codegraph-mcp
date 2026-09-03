"""Servidor MCP: expõe o grafo (indexado com `codegraph.cli`) como tools
pro Kimi Code chamar sob demanda, em vez de carregar o projeto inteiro na
janela de contexto.

DB usado é lido de $CODEGRAPH_DB (default: ./graph.db, relativo ao `cwd`
que o mcp.json configurar pra esse servidor).
"""

import os

from mcp.server.mcpserver import MCPServer

from codegraph import db

DB_PATH = os.environ.get("CODEGRAPH_DB", "graph.db")

mcp = MCPServer("codegraph")


def _conn():
    return db.connect(DB_PATH)


def _node_summary(row) -> dict:
    d = db.row_to_dict(row)
    return {"id": d["id"], "type": d["type"], "name": d["name"], "path": d.get("path")}


@mcp.tool()
def list_files() -> list[dict]:
    """Lista todos os arquivos indexados (nós tipo `file`), sem conteúdo."""
    conn = _conn()
    rows = db.get_roots(conn, "file")
    return [_node_summary(r) for r in rows]


@mcp.tool()
def get_file_tree(path: str) -> dict:
    """Dado um path de arquivo (relativo à raiz indexada), devolve o nó
    `file` e a lista dos seus `file_context` filhos (nome + linhas, sem
    o conteúdo inteiro -- use get_node pra pegar o conteúdo de um contexto
    específico)."""
    conn = _conn()
    file_row = db.find_node(conn, type="file", path=path)
    if file_row is None:
        return {"error": f"arquivo não encontrado no grafo: {path}"}
    children = db.get_children(conn, file_row["id"])
    return {
        "file": _node_summary(file_row),
        "contexts": [
            {"id": c["id"], "name": c["name"], "start_line": c["start_line"], "end_line": c["end_line"]}
            for c in children
        ],
    }


@mcp.tool()
def get_node(node_id: int) -> dict:
    """Devolve um nó completo (com conteúdo), seus filhos (resumo) e as
    arestas ligadas a ele (ex: um flow_step ligado ao file_context que o
    implementa, ou vice-versa)."""
    conn = _conn()
    row = db.get_node(conn, node_id)
    if row is None:
        return {"error": f"nó não encontrado: {node_id}"}
    children = db.get_children(conn, node_id)
    edges_out = db.get_edges_from(conn, node_id)
    edges_in = db.get_edges_to(conn, node_id)
    return {
        "node": db.row_to_dict(row),
        "children": [_node_summary(c) for c in children],
        "related": {
            "outgoing": [
                {"type": e["type"], "target": _node_summary(db.get_node(conn, e["dst_id"]))}
                for e in edges_out
            ],
            "incoming": [
                {"type": e["type"], "source": _node_summary(db.get_node(conn, e["src_id"]))}
                for e in edges_in
            ],
        },
    }


@mcp.tool()
def search(query: str, limit: int = 20) -> list[dict]:
    """Busca full-text por nome/conteúdo entre todos os nós (arquivos,
    contextos, fluxos e passos de fluxo). `query` aceita sintaxe FTS5
    (ex: "auth AND token")."""
    conn = _conn()
    rows = db.search(conn, query, limit)
    return [_node_summary(r) for r in rows]


@mcp.tool()
def list_flows() -> list[dict]:
    """Lista todos os fluxos de lógica carregados (nós tipo `flow`)."""
    conn = _conn()
    rows = db.get_roots(conn, "flow")
    return [{"id": r["id"], "name": r["name"], "description": r["content"]} for r in rows]


@mcp.tool()
def get_flow(name: str) -> dict:
    """Devolve um fluxo completo: seus passos em ordem, e pra cada passo,
    os nós file_context/file que o implementam (já resolvidos, com
    conteúdo) -- é o atalho pra 'já sei como esse fluxo funciona' sem
    precisar reconstruir isso lendo os arquivos de novo."""
    conn = _conn()
    flow_row = conn.execute("SELECT * FROM nodes WHERE type='flow' AND name=?", (name,)).fetchone()
    if flow_row is None:
        return {"error": f"fluxo não encontrado: {name}"}
    steps = db.get_children(conn, flow_row["id"])
    out_steps = []
    for step in steps:
        edges = db.get_edges_from(conn, step["id"])
        implements = []
        for e in edges:
            target = db.get_node(conn, e["dst_id"])
            implements.append(db.row_to_dict(target))
        out_steps.append({
            "name": step["name"],
            "description": step["content"],
            "implements": implements,
        })
    return {
        "name": flow_row["name"],
        "description": flow_row["content"],
        "steps": out_steps,
    }


if __name__ == "__main__":
    mcp.run()
