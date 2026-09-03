"""Qual projeto está "ativo" agora -- é pra lá que o proxy (proxy.py)
grava o histórico de prompt+resposta, já que só existe um llama-server
rodando por vez, compartilhado entre qualquer projeto que o Kimi Code
tenha aberto.
"""

from pathlib import Path

STATE_DIR = Path.home() / ".codegraph"
ACTIVE_PROJECT_FILE = STATE_DIR / "active-project"


def get_active_project() -> Path | None:
    if not ACTIVE_PROJECT_FILE.exists():
        return None
    raw = ACTIVE_PROJECT_FILE.read_text().strip()
    return Path(raw) if raw else None


def set_active_project(project_root: Path) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROJECT_FILE.write_text(f"{project_root}\n")
