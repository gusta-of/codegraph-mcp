#!/bin/bash
# Atalho pra quem prefere Bash -- equivalente a `codegraph setup <projeto>`.
# Funciona em Linux/macOS (ou Windows com Git Bash/WSL). Pra Windows nativo
# (PowerShell/cmd), use `codegraph setup <projeto>` direto -- ver INSTALL.md.
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

if [ ! -x "$PYTHON" ]; then
  echo "erro: venv nao encontrado em $PYTHON -- rode a instalacao do INSTALL.md primeiro" >&2
  exit 1
fi

"$PYTHON" -m codegraph.cli setup "$1"
