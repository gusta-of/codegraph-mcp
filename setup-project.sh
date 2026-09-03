#!/bin/bash
# Indexa um projeto e registra o codegraph-mcp nele -- as partes 1 e 3
# do fluxo manual (ver ARQUITETURA.md secao 7), num comando so.
#
# Uso:
#   ./setup-project.sh /caminho/do/projeto
set -euo pipefail

CODEGRAPH_MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$CODEGRAPH_MCP_DIR/.venv/bin/python"

if [ -z "${1:-}" ]; then
  echo "uso: $0 /caminho/do/projeto" >&2
  exit 1
fi
PROJECT_DIR="$(cd "$1" && pwd)"

if [ ! -x "$PYTHON" ]; then
  echo "erro: venv nao encontrado em $PYTHON -- rode a instalacao do README.md primeiro" >&2
  exit 1
fi

DB_PATH="$PROJECT_DIR/.codegraph/graph.db"
mkdir -p "$PROJECT_DIR/.codegraph"

echo "==> indexando $PROJECT_DIR"
"$PYTHON" -m codegraph.cli --db "$DB_PATH" index "$PROJECT_DIR"

FLOWS_DIR="$PROJECT_DIR/.codegraph/flows"
if [ -d "$FLOWS_DIR" ] && [ -n "$(ls -A "$FLOWS_DIR" 2>/dev/null)" ]; then
  echo "==> carregando fluxos de $FLOWS_DIR"
  "$PYTHON" -m codegraph.cli --db "$DB_PATH" load-flows "$FLOWS_DIR"
else
  echo "==> nenhum fluxo em $FLOWS_DIR (pulado -- crie .yaml la e rode de novo quando quiser)"
fi

echo "==> registrando em $PROJECT_DIR/.kimi-code/mcp.json"
mkdir -p "$PROJECT_DIR/.kimi-code"
"$PYTHON" - "$PROJECT_DIR/.kimi-code/mcp.json" "$CODEGRAPH_MCP_DIR" "$DB_PATH" <<'PYEOF'
# Merge sem apagar outras entradas mcpServers que ja existam no arquivo.
import json
import sys
from pathlib import Path

mcp_json_path, codegraph_dir, db_path = sys.argv[1], sys.argv[2], sys.argv[3]
path = Path(mcp_json_path)

data = {}
if path.exists():
    data = json.loads(path.read_text())

data.setdefault("mcpServers", {})["codegraph"] = {
    "command": f"{codegraph_dir}/.venv/bin/python",
    "args": ["-m", "codegraph.server"],
    "cwd": codegraph_dir,
    "env": {"CODEGRAPH_DB": db_path},
}

path.write_text(json.dumps(data, indent=2) + "\n")
PYEOF

echo "==> config de historico ($PROJECT_DIR/.kimi-code/codegraph-history.json)"
"$PYTHON" -c "
from pathlib import Path
from codegraph import history
path = history.write_default_config(Path('$PROJECT_DIR'))
print(f'   {path} (nao mexi se ja existia)')
"

echo "==> marcando $PROJECT_DIR como projeto ativo (recebe o historico de prompts agora)"
"$PYTHON" -c "
from pathlib import Path
from codegraph import state
state.set_active_project(Path('$PROJECT_DIR'))
"

echo ""
echo "pronto. proximos passos:"
echo "  1. abrir uma sessao NOVA do Kimi Code dentro de $PROJECT_DIR"
echo "     (sessao ja aberta antes deste comando nao pega o mcp.json sozinha)"
echo "  2. dentro do Kimi Code, rodar /mcp pra confirmar a conexao"
echo "  3. o proxy de historico (codegraph.proxy) precisa estar rodando"
echo "     e o Kimi Code apontando pra ele -- ver ARQUITETURA.md"
