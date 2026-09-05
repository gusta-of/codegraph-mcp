"""CLI pra indexar um projeto e carregar fluxos, sem precisar do servidor MCP rodando.

Uso:
    python -m codegraph.cli index /caminho/do/projeto [--db graph.db]
    python -m codegraph.cli load-flows ./flows [--db graph.db]
    python -m codegraph.cli stats [--db graph.db]
    python -m codegraph.cli setup /caminho/do/projeto

`setup` é o equivalente em Python puro do `setup-project.sh` -- roda
igual em Windows/macOS/Linux (o `.sh` só funciona onde tem Bash: Linux,
macOS, ou Windows com Git Bash/WSL).
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from codegraph import db, flows, history, indexer, state


def cmd_index(args):
    conn = db.connect(args.db)
    print(f"Indexando {args.root} -> {args.db}")
    stats = indexer.index_project(conn, Path(args.root))
    print(
        f"\nfeito: {stats['files_indexed']} indexado(s), "
        f"{stats['files_unchanged']} sem mudança, "
        f"{stats['files_skipped']} pulado(s) (binário/grande), "
        f"{stats['contexts_created']} contexto(s) criado(s)"
        + (f", {stats['files_ignored_removed']} removido(s) por .codegraphignore" if stats['files_ignored_removed'] else "")
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


def _write_mcp_json(project_dir: Path, codegraph_dir: Path, db_path: Path) -> Path:
    """Registra/atualiza a entrada "codegraph" em .kimi-code/mcp.json, sem
    apagar outras entradas mcpServers que já estejam lá. `sys.executable`
    (o interpretador que está rodando este script agora) já resolve certo
    em qualquer SO -- não precisa adivinhar `.venv/bin/python` (Unix) vs
    `.venv\\Scripts\\python.exe` (Windows)."""
    mcp_json_path = project_dir / ".kimi-code" / "mcp.json"
    mcp_json_path.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if mcp_json_path.exists():
        data = json.loads(mcp_json_path.read_text())

    data.setdefault("mcpServers", {})["codegraph"] = {
        "command": sys.executable,
        "args": ["-m", "codegraph.server"],
        "cwd": str(codegraph_dir),
        "env": {"CODEGRAPH_DB": str(db_path)},
    }
    mcp_json_path.write_text(json.dumps(data, indent=2) + "\n")
    return mcp_json_path


_AGENTS_MD_MARKER_START = "<!-- codegraph-mcp: início do bloco automático -->"
_AGENTS_MD_MARKER_END = "<!-- codegraph-mcp: fim do bloco automático -->"
# Minimizado de propósito (2026-09-05, pedido do usuário) -- esse bloco
# entra no contexto de TODA mensagem, não só quando a tool é usada; a
# versão longa anterior (~12 linhas com explicação) tinha custo fixo em
# toda troca pra transmitir a mesma instrução que cabe numa frase. Ver
# ECONOMIA_DE_TOKENS.md.
_AGENTS_MD_BLOCK = (
    f"{_AGENTS_MD_MARKER_START}\n"
    "Antes de ler arquivo inteiro ou usar Grep no projeto todo, prefira "
    "`mcp__codegraph__search`/`get_flow`/`list_history` -- consulte "
    "primeiro, leia o arquivo só se não for suficiente.\n"
    f"{_AGENTS_MD_MARKER_END}\n"
)


def _write_agents_md(project_dir: Path) -> Path:
    """Escreve/atualiza um bloco em .kimi-code/AGENTS.md orientando o
    agente a preferir as tools do codegraph em vez de ler arquivo inteiro
    por padrão. Sem isso, a tool aparece disponível pro agente (confirmado
    rodando `kimi -p "liste suas tools"`) mas ele nunca escolhe chamá-la
    sozinho -- 0% de uso em dezenas de trocas reais, achado real
    2026-09-05, ver ARQUITETURA.md.

    Idempotente igual `_write_mcp_json`: se o bloco já existe (marcadores
    presentes), SUBSTITUI só o conteúdo entre eles -- não duplica, e runs
    futuros de `setup` pegam mudança no template (ex: essa minimização)
    sem precisar apagar o arquivo na mão. Nunca mexe em nada que o usuário
    tenha escrito fora dos marcadores."""
    path = project_dir / ".kimi-code" / "AGENTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if _AGENTS_MD_MARKER_START in existing and _AGENTS_MD_MARKER_END in existing:
        start = existing.index(_AGENTS_MD_MARKER_START)
        end = existing.index(_AGENTS_MD_MARKER_END) + len(_AGENTS_MD_MARKER_END)
        path.write_text(existing[:start] + _AGENTS_MD_BLOCK.rstrip("\n") + existing[end:])
        return path
    separator = "\n" if existing and not existing.endswith("\n") else ""
    path.write_text(existing + separator + ("\n" if existing else "") + _AGENTS_MD_BLOCK)
    return path


def _write_reindex_launcher(project_dir: Path, codegraph_dir: Path) -> Path:
    """Cria, dentro de .codegraph/, um arquivo executável direto pelo SO
    (sem precisar lembrar comando/caminho) que reindexa ESTE projeto de
    novo -- pra rodar toda vez que criar/mudar arquivos e quiser atualizar
    o grafo. Gera o formato certo pro SO atual (`.sh` em Linux/macOS,
    `.bat` no Windows) -- só um dos dois, o da máquina onde `setup` rodou."""
    codegraph_subdir = project_dir / ".codegraph"
    codegraph_subdir.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        path = codegraph_subdir / "reindex.bat"
        path.write_text(
            "@echo off\r\n"
            "REM Gerado por `codegraph setup` -- reindexar este projeto de novo.\r\n"
            "REM Roda toda vez que criar/mudar arquivos e quiser atualizar o grafo.\r\n"
            f'"{sys.executable}" -m codegraph.cli setup "{project_dir}"\r\n'
            "pause\r\n"
        )
        return path

    path = codegraph_subdir / "reindex.sh"
    path.write_text(
        "#!/bin/bash\n"
        "# Gerado por `codegraph setup` -- reindexar este projeto de novo.\n"
        "# Roda toda vez que criar/mudar arquivos e quiser atualizar o grafo.\n"
        f'"{sys.executable}" -m codegraph.cli setup "{project_dir}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def cmd_setup(args):
    codegraph_dir = Path(__file__).resolve().parent.parent
    project_dir = Path(args.project).resolve()
    db_path = project_dir / ".codegraph" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"==> indexando {project_dir}")
    conn = db.connect(str(db_path))
    stats = indexer.index_project(conn, project_dir)
    print(
        f"    {stats['files_indexed']} indexado(s), {stats['files_unchanged']} sem mudança, "
        f"{stats['files_skipped']} pulado(s), {stats['contexts_created']} contexto(s) criado(s)"
        + (f", {stats['files_ignored_removed']} removido(s) por .codegraphignore" if stats['files_ignored_removed'] else "")
    )

    flows_dir = project_dir / ".codegraph" / "flows"
    yaml_files = list(flows_dir.glob("*.yaml")) + list(flows_dir.glob("*.yml")) if flows_dir.is_dir() else []
    if yaml_files:
        print(f"==> carregando fluxos de {flows_dir}")
        flows.load_flows_dir(conn, flows_dir)
    else:
        print(f"==> nenhum fluxo em {flows_dir} (pulado -- crie .yaml lá e rode de novo quando quiser)")

    mcp_json_path = _write_mcp_json(project_dir, codegraph_dir, db_path)
    print(f"==> registrado em {mcp_json_path}")

    agents_md_path = _write_agents_md(project_dir)
    print(f"==> instrução de uso em {agents_md_path} (orienta o agente a preferir as tools do codegraph)")

    history_config_path = history.write_default_config(project_dir)
    print(f"==> config de histórico: {history_config_path} (não mexi se já existia)")

    state.set_active_project(project_dir)
    print(f"==> {project_dir} marcado como projeto ativo (recebe o histórico de prompts agora)")

    launcher_path = _write_reindex_launcher(project_dir, codegraph_dir)
    print(f"==> criado {launcher_path} -- roda ele (clique duplo, ou no terminal) toda vez que quiser reindexar esse projeto de novo")

    print()
    print("pronto. próximos passos:")
    print(f"  1. abrir uma sessão NOVA do Kimi Code dentro de {project_dir}")
    print("     (sessão já aberta antes deste comando não pega o mcp.json sozinha)")
    print("  2. dentro do Kimi Code, rodar /mcp pra confirmar a conexão")
    print("  3. servidor de modelo + proxy precisam estar rodando -- ver INSTALL.md")
    print(f"  4. daqui pra frente, pra atualizar o grafo, roda {launcher_path.name} -- não precisa lembrar o comando completo de novo")


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

    p_setup = sub.add_parser("setup", help="indexa + registra mcp.json + config de histórico, tudo de uma vez (equivalente Python do setup-project.sh)")
    p_setup.add_argument("project", help="raiz do projeto a configurar")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
