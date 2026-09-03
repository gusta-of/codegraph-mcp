"""CLI pra indexar um projeto e carregar fluxos, sem precisar do servidor MCP rodando.

Uso:
    python -m codegraph.cli index /caminho/do/projeto [--db graph.db]
    python -m codegraph.cli load-flows ./flows [--db graph.db]
    python -m codegraph.cli stats [--db graph.db]
"""

import argparse
from pathlib import Path

from codegraph import db, flows, indexer


def cmd_index(args):
    conn = db.connect(args.db)
    print(f"Indexando {args.root} -> {args.db}")
    stats = indexer.index_project(conn, Path(args.root))
    print(
        f"\nfeito: {stats['files_indexed']} indexado(s), "
        f"{stats['files_unchanged']} sem mudança, "
        f"{stats['files_skipped']} pulado(s) (binário/grande), "
        f"{stats['contexts_created']} contexto(s) criado(s)"
    )


def cmd_load_flows(args):
    conn = db.connect(args.db)
    print(f"Carregando fluxos de {args.flows_dir} -> {args.db}")
    flows.load_flows_dir(conn, Path(args.flows_dir))


def cmd_stats(args):
    conn = db.connect(args.db)
    for t in ("file", "file_context", "flow", "flow_step"):
        n = conn.execute("SELECT COUNT(*) FROM nodes WHERE type=?", (t,)).fetchone()[0]
        print(f"  {t:14s} {n}")
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"  {'edges':14s} {n_edges}")


def main():
    parser = argparse.ArgumentParser(prog="codegraph")
    parser.add_argument("--db", default="graph.db", help="caminho do arquivo SQLite (default: ./graph.db)")
    sub = parser.add_subparsers(required=True)

    p_index = sub.add_parser("index", help="varre um diretório e indexa arquivos+contexto")
    p_index.add_argument("root", help="raiz do projeto a indexar")
    p_index.set_defaults(func=cmd_index)

    p_flows = sub.add_parser("load-flows", help="carrega fluxos YAML no grafo")
    p_flows.add_argument("flows_dir", help="diretório com os .yaml de fluxo")
    p_flows.set_defaults(func=cmd_load_flows)

    p_stats = sub.add_parser("stats", help="mostra contagem de nós/arestas no grafo")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
