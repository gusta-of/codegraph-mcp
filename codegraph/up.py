"""Sobe o servidor de modelo (llama-server) + o proxy de histórico, num
comando só -- multiplataforma (Windows/macOS/Linux), tudo configurado por
variável de ambiente. Nada de caminho fixo de uma máquina específica.

Uso:
    python -m codegraph.up
    # ou, depois de `pip install -e .`:
    codegraph-up

Variáveis de ambiente:
    CODEGRAPH_MODEL_PATH        (obrigatória) caminho do arquivo .gguf
    CODEGRAPH_LLAMA_SERVER_BIN  (default: "llama-server" -- precisa estar no PATH)
    CODEGRAPH_LLAMA_PORT        (default: 8080)
    CODEGRAPH_LLAMA_ARGS        (default: flags genéricas e conservadoras)
    CODEGRAPH_PROXY_PORT        (default: 8081)
"""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import httpx

CODEGRAPH_DIR = Path(__file__).resolve().parent.parent

# Conservador de propósito: -c pequeno funciona em qualquer máquina
# (pouca VRAM/RAM), --reasoning-budget evita loop de raciocínio infinito
# (ver ARQUITETURA.md). Ajustar via CODEGRAPH_LLAMA_ARGS pro hardware real.
DEFAULT_LLAMA_ARGS = "-ngl 999 -fa on -c 8192 --no-prefill-assistant --reasoning-budget 16000"


def _is_up(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def _spawn(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w")
    kwargs: dict = {"stdout": log_file, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _wait_up(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_up(port):
            return True
        time.sleep(1.0)
    return False


def start_llama_server() -> bool:
    port = int(os.environ.get("CODEGRAPH_LLAMA_PORT", "8080"))
    if _is_up(port):
        print(f"[llama-server] já está rodando (porta {port})")
        return True

    model_path = os.environ.get("CODEGRAPH_MODEL_PATH")
    if not model_path:
        print(
            "ERRO: variável de ambiente CODEGRAPH_MODEL_PATH não configurada "
            "-- aponte pro seu arquivo .gguf. Sem isso o servidor não sobe. "
            "Ver INSTALL.md.",
            file=sys.stderr,
        )
        return False
    if not Path(model_path).exists():
        print(f"ERRO: modelo não encontrado em: {model_path}", file=sys.stderr)
        return False

    bin_name = os.environ.get("CODEGRAPH_LLAMA_SERVER_BIN", "llama-server")
    extra_args = shlex.split(os.environ.get("CODEGRAPH_LLAMA_ARGS", DEFAULT_LLAMA_ARGS))
    cmd = [bin_name, "-m", model_path, "--port", str(port), *extra_args]
    log_path = CODEGRAPH_DIR / "logs" / "llama-server.log"

    print(f"[llama-server] subindo: {' '.join(cmd)}")
    try:
        _spawn(cmd, log_path)
    except FileNotFoundError:
        print(
            f"ERRO: binário '{bin_name}' não encontrado -- precisa estar no "
            "PATH (ou configure CODEGRAPH_LLAMA_SERVER_BIN com o caminho "
            "completo do executável). Ver INSTALL.md.",
            file=sys.stderr,
        )
        return False

    ok = _wait_up(port, timeout=120.0)
    if ok:
        print(f"[llama-server] no ar (porta {port}) -- log: {log_path}")
    else:
        print(f"[llama-server] não respondeu em 120s -- confere o log: {log_path}", file=sys.stderr)
    return ok


def start_proxy() -> bool:
    port = int(os.environ.get("CODEGRAPH_PROXY_PORT", "8081"))
    if _is_up(port):
        print(f"[codegraph-proxy] já está rodando (porta {port})")
        return True

    cmd = [sys.executable, "-m", "codegraph.proxy"]
    log_path = CODEGRAPH_DIR / "logs" / "codegraph-proxy.log"

    print("[codegraph-proxy] subindo")
    _spawn(cmd, log_path)

    ok = _wait_up(port, timeout=20.0)
    if ok:
        print(f"[codegraph-proxy] no ar (porta {port}) -- log: {log_path}")
    else:
        print(f"[codegraph-proxy] não respondeu em 20s -- confere o log: {log_path}", file=sys.stderr)
    return ok


def main():
    ok_llama = start_llama_server()
    ok_proxy = start_proxy()
    if not (ok_llama and ok_proxy):
        print("\nnem tudo subiu -- ver mensagens de erro acima e INSTALL.md.", file=sys.stderr)
        sys.exit(1)
    print("\ntudo no ar. pode abrir o Kimi Code agora.")


if __name__ == "__main__":
    main()
