"""Proxy HTTP entre o Kimi Code e o llama-server.

Repassa toda requisição pro llama-server de verdade (byte a byte no
streaming -- não reformata nada, só observa uma cópia por cima), e grava
cada prompt+resposta de `/v1/chat/completions` como nó `history` no
grafo do projeto ATIVO no momento (ver codegraph/state.py).

Kimi Code precisa apontar pra cá em vez de direto no llama-server:
`base_url` em ~/.kimi-code/config.toml -> http://localhost:8081/v1
(ver ARQUITETURA.md, seção de histórico).
"""

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from codegraph import db, history, indexer, metrics, state

UPSTREAM = os.environ.get("CODEGRAPH_UPSTREAM", "http://127.0.0.1:8080")
LISTEN_PORT = int(os.environ.get("CODEGRAPH_PROXY_PORT", "8081"))

client = httpx.AsyncClient(timeout=None)

# Uma lock por projeto -- se uma troca terminar (e disparar reindex) antes da
# reindexação anterior acabar, pula em vez de empilhar rodadas concorrentes
# do indexador no mesmo .db (achado real, 2026-09-05: reindex fica automático
# a cada troca completa, ver ARQUITETURA.md).
_reindex_locks: dict[str, asyncio.Lock] = {}

# Acumula tool do codegraph usada em QUALQUER rodada intermediária do loop de
# tool-calling, indexado pela pergunta real (_last_user_message) -- uma
# pergunta gera várias requisições HTTP separadas (uma por rodada), cada uma
# com seu próprio `used_tools` do zero. Sem acumular fora do request, a
# métrica de uso nunca detectava nada: a resposta FINAL (a única que vira
# nó de histórico -- ver has_tool_call em _log_exchange) por definição nunca
# tem tool-call nela mesma, então o `used_tools` daquela requisição
# específica sempre vinha vazio, mesmo quando uma rodada anterior tinha
# chamado `search` de verdade (achado real, 2026-09-05, ver ARQUITETURA.md).
_pending_tool_usage: dict[str, set[str]] = {}


def _reindex_sync(project_root: Path, db_path: Path) -> None:
    conn = db.connect(str(db_path))
    try:
        indexer.index_project(conn, project_root, verbose=False)
    finally:
        conn.close()


async def _auto_reindex(project_root: Path, db_path: Path) -> None:
    """Reindexa sozinho depois de cada troca completa -- sem isso, a árvore
    só refletia edições depois de rodar `.codegraph/reindex.sh` na mão. O
    indexador já pula arquivo sem mudança de hash (seção 2.5 do
    ARQUITETURA.md), então rodar de novo sem nada ter mudado é barato --
    ainda assim roda em thread separada (`asyncio.to_thread`) pra não travar
    a resposta que já foi mandada de volta pro Kimi Code, e nunca duas vezes
    em paralelo pro mesmo projeto."""
    lock = _reindex_locks.setdefault(str(project_root), asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        await asyncio.to_thread(_reindex_sync, project_root, db_path)

# Nomes das tools do codegraph-mcp -- usado só pra saber se uma troca usou
# alguma delas (métricas de efetividade, ver metrics.py/rota /dashboard).
CODEGRAPH_TOOL_NAMES = {
    "list_files", "get_file_tree", "get_node", "search",
    "list_flows", "get_flow", "list_history",
}


def _is_codegraph_tool(name: str) -> bool:
    """Kimi Code (e provavelmente outros clientes MCP) prefixam o nome da
    tool com o nome do servidor pra evitar colisão entre MCP servers
    diferentes -- ex: "search" vira "mcp__codegraph__search". Bater só
    contra CODEGRAPH_TOOL_NAMES (nome puro) nunca reconhece isso -- foi
    um bug real, descoberto testando ao vivo (2026-09-03): a tool era
    chamada de verdade mas o dashboard nunca contava, porque a checagem
    de nome sempre falhava. "codegraph" no nome (nosso servidor MCPServer
    se chama exatamente isso, ver server.py) é o sinal robusto,
    independente de qual convenção de prefixo o cliente usar."""
    return "codegraph" in name.lower() or name in CODEGRAPH_TOOL_NAMES


_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# Mensagens sintéticas que o Kimi Code injeta como role=user mas que não são
# uma pergunta humana -- não vêm dentro de <system-reminder>...</system-reminder>
# (por isso o regex acima não pega), são texto plano de housekeeping interno
# (handoff de compactação de contexto). Achado real, 2026-09-05: apareceram
# como "prompt" de nós de histórico na árvore (ids 201-203, projeto
# royal_poker_online) -- mesmo problema do system-reminder, forma diferente.
_SYNTHETIC_PROMPT_PREFIXES = (
    "You are about to run out of context.",
    "The conversation so far has been compacted",
)


def _strip_system_reminders(text: str) -> str:
    return _SYSTEM_REMINDER_RE.sub("", text or "").strip()


def _is_synthetic_prompt(text: str) -> bool:
    return text.strip().startswith(_SYNTHETIC_PROMPT_PREFIXES)


def _last_user_message(messages: list) -> str:
    """Acha a última pergunta REAL do usuário. Kimi Code injeta mensagens
    role=user cujo conteúdo é só um `<system-reminder>...</system-reminder>`
    (lembrete de data, de todo list, etc) a cada rodada interna do loop de
    tool-calling -- sem filtrar isso, cada rodada interna virava um nó de
    histórico com "prompt" = lembrete/lixo em vez da pergunta humana real
    (achado real, 2026-09-05, print do usuário mostrando isso na árvore --
    ver ARQUITETURA.md)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            text = "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = content or ""
        cleaned = _strip_system_reminders(text)
        if cleaned and not _is_synthetic_prompt(cleaned):
            return cleaned
    return ""


def _extract_delta(chunk: dict) -> tuple[str, str, list[str]]:
    """Devolve (content, reasoning_content, nomes de tool chamada) de um
    chunk de streaming -- tool_calls vem parcial (por índice), mas o nome
    da função normalmente já vem completo no primeiro chunk daquele índice."""
    try:
        delta = chunk["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return "", "", []
    tool_names = [
        tc["function"]["name"]
        for tc in (delta.get("tool_calls") or [])
        if isinstance(tc, dict) and tc.get("function", {}).get("name")
    ]
    return delta.get("content") or "", delta.get("reasoning_content") or "", tool_names


async def _log_exchange(
    prompt: str, content: str, reasoning: str,
    usage: dict | None, timings: dict | None, used_tools: set[str],
    has_tool_call: bool = False,
) -> None:
    if not prompt.strip() or not content.strip() or has_tool_call:
        # Exchange incompleta -- rodada intermediária do loop de tool-calling
        # (resposta virou só tool_calls, sem texto final, ou narrou algo tipo
        # "deixa eu checar o arquivo..." JUNTO com uma tool-call -- ainda não
        # é a resposta final) ou uma chamada cujo "último user message" era só
        # lembrete/reminder sem pergunta humana de verdade (ver
        # _last_user_message). Gravar isso é jogar lixo na memória -- só a
        # resposta final, sem tool-call pendente, conta como troca completa
        # (achado real, 2026-09-05, ver ARQUITETURA.md).
        return
    # Junta com o que foi acumulado nas rodadas intermediárias dessa mesma
    # pergunta (_pending_tool_usage) -- o `used_tools` desta chamada
    # específica está vazio quase sempre (é a resposta final, sem tool-call
    # nela mesma), quem tem o uso real são as rodadas anteriores.
    used_tools = used_tools | _pending_tool_usage.pop(prompt, set())
    active = state.get_active_project()
    if active is None:
        return  # nenhum projeto marcado como ativo -- so' passa direto, sem log
    db_path = active / ".codegraph" / "graph.db"
    if not db_path.exists():
        return
    max_bytes = history.load_max_bytes(active)
    conn = db.connect(str(db_path))
    try:
        meta = {"reasoning_chars": len(reasoning)}
        if usage:
            meta["prompt_tokens"] = usage.get("prompt_tokens")
            meta["completion_tokens"] = usage.get("completion_tokens")
            meta["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if timings:
            meta["predicted_per_second"] = timings.get("predicted_per_second")
            meta["prompt_per_second"] = timings.get("prompt_per_second")
        if used_tools:
            meta["used_codegraph_tools"] = sorted(used_tools)
        history.record_exchange(
            conn, str(db_path), prompt=prompt, response=content,
            max_bytes=max_bytes, metadata=meta,
        )
    finally:
        conn.close()
    asyncio.create_task(_auto_reindex(active, db_path))


async def chat_completions(request: Request):
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    prompt = _last_user_message(payload.get("messages", []))
    is_stream = bool(payload.get("stream"))

    if not is_stream:
        resp = await client.post(
            f"{UPSTREAM}/v1/chat/completions", content=body,
            headers={"content-type": "application/json"},
        )
        content, reasoning, used_tools = "", "", set()
        usage = None
        has_tool_call = False
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            has_tool_call = bool(msg.get("tool_calls"))
            used_tools = {
                tc["function"]["name"] for tc in (msg.get("tool_calls") or [])
                if _is_codegraph_tool(tc.get("function", {}).get("name") or "")
            }
            usage = data.get("usage")
        except (KeyError, IndexError, ValueError):
            pass
        if used_tools and prompt:
            _pending_tool_usage.setdefault(prompt, set()).update(used_tools)
        asyncio.create_task(_log_exchange(
            prompt, content, reasoning, usage, None, used_tools, has_tool_call,
        ))
        return Response(
            resp.content, status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    # pede usage no ultimo chunk do streaming -- o llama-server so manda
    # se pedido explicitamente (testado: sem isso, o campo "usage" nao vem).
    payload.setdefault("stream_options", {})["include_usage"] = True
    body = json.dumps(payload).encode()

    upstream_req = client.build_request(
        "POST", f"{UPSTREAM}/v1/chat/completions", content=body,
        headers={"content-type": "application/json"},
    )
    upstream_resp = await client.send(upstream_req, stream=True)
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    used_tools: set[str] = set()
    usage_holder: dict = {}
    timings_holder: dict = {}
    has_tool_call = False

    async def gen():
        nonlocal has_tool_call
        buffer = b""
        async for raw_chunk in upstream_resp.aiter_bytes():
            yield raw_chunk  # passthrough exato -- nunca reformata o que o cliente ve
            buffer += raw_chunk
            while b"\n\n" in buffer:
                event, buffer = buffer.split(b"\n\n", 1)
                for line in event.split(b"\n"):
                    if not line.startswith(b"data: "):
                        continue
                    data_str = line[len(b"data: "):].decode("utf-8", errors="ignore")
                    if data_str.strip() == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage_holder.update(chunk["usage"])
                    if chunk.get("timings"):
                        timings_holder.update(chunk["timings"])
                    c, r, tools = _extract_delta(chunk)
                    if c:
                        content_parts.append(c)
                    if r:
                        reasoning_parts.append(r)
                    if tools:
                        has_tool_call = True
                    for name in tools:
                        if _is_codegraph_tool(name):
                            used_tools.add(name)
        await upstream_resp.aclose()
        if used_tools and prompt:
            _pending_tool_usage.setdefault(prompt, set()).update(used_tools)
        asyncio.create_task(_log_exchange(
            prompt, "".join(content_parts), "".join(reasoning_parts),
            usage_holder or None, timings_holder or None, used_tools, has_tool_call,
        ))

    return StreamingResponse(gen(), media_type="text/event-stream")


def _resolve_project(request: Request) -> Path | None:
    project_param = request.query_params.get("project")
    return Path(project_param) if project_param else state.get_active_project()


def _resolve_db_path(request: Request) -> tuple[Path | None, Path | None]:
    """Devolve (project_dir, db_path) -- qualquer um pode vir None se não
    tiver projeto ativo/configurado ou o grafo ainda não existir."""
    project_dir = _resolve_project(request)
    if project_dir is None:
        return None, None
    db_path = project_dir / ".codegraph" / "graph.db"
    return project_dir, (db_path if db_path.exists() else None)


def _node_summary(row) -> dict:
    return {"id": row["id"], "type": row["type"], "name": row["name"], "path": row["path"]}


async def api_tree_roots(request: Request):
    project_dir, db_path = _resolve_db_path(request)
    if db_path is None:
        return JSONResponse({"error": "grafo não encontrado -- rode `codegraph setup` primeiro"}, status_code=404)
    conn = db.connect(str(db_path))
    try:
        roots = list(db.get_roots(conn, "file")) + list(db.get_roots(conn, "flow"))
        nodes = [_node_summary(r) for r in roots]
    finally:
        conn.close()
    return JSONResponse({"project": project_dir.name, "nodes": nodes})


async def api_history_config_get(request: Request):
    """Tamanho real do .db + teto configurado -- pra mostrar a estatística
    na aba Árvore (tamanho em MB/GB, % preenchido)."""
    project_dir, db_path = _resolve_db_path(request)
    if project_dir is None:
        return JSONResponse({"error": "nenhum projeto ativo"}, status_code=404)
    max_bytes = history.load_max_bytes(project_dir)
    size_bytes = db_path.stat().st_size if db_path is not None else 0
    return JSONResponse({
        "db_size_bytes": size_bytes,
        "max_history_mb": max_bytes // (1024 * 1024),
        "percent_used": round(100 * size_bytes / max_bytes, 1) if max_bytes else 0.0,
    })


async def api_history_config_set(request: Request):
    """Atualiza max_history_mb -- recusa (400) se for menor que o que o
    banco já ocupa hoje (history.LimitTooSmallError, ver history.py)."""
    project_dir = _resolve_project(request)
    if project_dir is None:
        return JSONResponse({"error": "nenhum projeto ativo"}, status_code=404)
    body = await request.json()
    try:
        mb = int(body.get("max_history_mb"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_history_mb precisa ser um número inteiro"}, status_code=400)
    if mb <= 0:
        return JSONResponse({"error": "max_history_mb precisa ser maior que zero"}, status_code=400)

    db_path = project_dir / ".codegraph" / "graph.db"
    try:
        history.set_max_mb(project_dir, mb, db_path)
    except history.LimitTooSmallError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse({"ok": True, "max_history_mb": mb})


async def api_health(request: Request):
    """Saúde agregada pra aba 'Saúde' do dashboard -- diferente da rota
    `/health` (seção 12.1 do ARQUITETURA.md), que só confirma o proxy em
    si pra scripts externos (`llama-qwen`). Aqui é o que uma pessoa
    olhando a tela precisa saber: o modelo (upstream) está respondendo?
    tem projeto ativo? o grafo dele existe/tem dado? tem reindexação
    rodando agora?"""
    project_dir, db_path = _resolve_db_path(request)
    active = state.get_active_project()

    try:
        r = await client.get(f"{UPSTREAM}/health", timeout=2.0)
        upstream = {"status": "ok" if r.status_code == 200 else "carregando", "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as e:
        upstream = {"status": "fora do ar", "detail": str(e)}

    db_info = {"exists": False}
    if db_path is not None:
        db_info = {"exists": True, "size_bytes": db_path.stat().st_size}
        conn = db.connect(str(db_path))
        try:
            db_info["counts"] = {
                t: conn.execute("SELECT COUNT(*) FROM nodes WHERE type=?", (t,)).fetchone()[0]
                for t in ("file", "file_context", "flow", "flow_step", "history")
            }
        finally:
            conn.close()

    reindexing = False
    if active is not None:
        lock = _reindex_locks.get(str(active))
        reindexing = bool(lock and lock.locked())

    return JSONResponse({
        "proxy": "ok",
        "upstream": {**upstream, "url": UPSTREAM},
        "active_project": str(active) if active else None,
        "db": db_info,
        "reindexing": reindexing,
    })


async def api_tree_history(request: Request):
    """Entradas de histórico (prompt+resposta) mais recentes -- separado
    de /api/tree/roots de propósito: history não tem hierarquia (parent_id
    sempre NULL, ver schema.sql seção 3) e pode ter muitas entradas (até o
    teto de tamanho configurado, ver history.py) -- listar tudo junto com
    file/flow na raiz replicaria o mesmo problema de escala que o
    agrupamento por pasta já resolveu pra arquivos. Vira um "balde"
    sintético só no front (`hist:` -- mesmo espírito de `dir:`).

    Paginado por cursor (`before_id`, não offset): mais recentes primeiro,
    "carregar mais antigas" pede a próxima página passando o menor `id` já
    visto. Sem isso, um projeto com muita conversa acumulada virava uma
    fileira só de dezenas de bolinhas na tela de uma vez (achado real,
    2026-09-05, print do usuário -- ver ARQUITETURA.md)."""
    project_dir, db_path = _resolve_db_path(request)
    if db_path is None:
        return JSONResponse({"error": "grafo não encontrado -- rode `codegraph setup` primeiro"}, status_code=404)
    limit = int(request.query_params.get("limit", "20"))
    before_id_raw = request.query_params.get("before_id")
    before_id = int(before_id_raw) if before_id_raw else None
    conn = db.connect(str(db_path))
    try:
        rows = db.get_recent(conn, "history", limit + 1, before_id=before_id)
        has_more = len(rows) > limit
        nodes = [_node_summary(r) for r in rows[:limit]]
    finally:
        conn.close()
    next_before_id = nodes[-1]["id"] if (has_more and nodes) else None
    return JSONResponse({"nodes": nodes, "has_more": has_more, "next_before_id": next_before_id})


async def api_tree_node(request: Request):
    project_dir, db_path = _resolve_db_path(request)
    if db_path is None:
        return JSONResponse({"error": "grafo não encontrado -- rode `codegraph setup` primeiro"}, status_code=404)
    node_id = request.path_params["node_id"]
    conn = db.connect(str(db_path))
    try:
        row = db.get_node(conn, node_id)
        if row is None:
            return JSONResponse({"error": f"nó não encontrado: {node_id}"}, status_code=404)
        children = db.get_children(conn, node_id)
        edges_out = db.get_edges_from(conn, node_id)
        edges_in = db.get_edges_to(conn, node_id)
        payload = {
            "node": db.row_to_dict(row),
            "children": [_node_summary(c) for c in children],
            "related_out": [
                {"type": e["type"], "target": _node_summary(db.get_node(conn, e["dst_id"]))}
                for e in edges_out
            ],
            "related_in": [
                {"type": e["type"], "source": _node_summary(db.get_node(conn, e["src_id"]))}
                for e in edges_in
            ],
        }
    finally:
        conn.close()
    return JSONResponse(payload)


def _render_dashboard(
    project_name: str, project_query: str, coverage: dict, effectiveness: dict, hist: list[dict],
) -> str:
    labels = [h["created_at"] for h in hist]
    prompt_tok = [h.get("prompt_tokens") for h in hist]
    completion_tok = [h.get("completion_tokens") for h in hist]
    speed = [h.get("predicted_per_second") for h in hist]

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>codegraph-mcp -- {project_name}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/styles/vis-network.min.css">
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f1117; color: #e6e6e6; margin: 0; padding: 24px; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
  .sub {{ color: #9aa0ac; margin-bottom: 16px; font-size: 0.85rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 28px; }}
  .card {{ background: #1a1d27; border: 1px solid #2a2e3a; border-radius: 10px; padding: 14px 16px; }}
  .card .n {{ font-size: 1.6rem; font-weight: 600; }}
  .card .l {{ font-size: 0.78rem; color: #9aa0ac; margin-top: 2px; }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .chart-box {{ background: #1a1d27; border: 1px solid #2a2e3a; border-radius: 10px; padding: 16px; }}
  .note {{ font-size: 0.75rem; color: #9aa0ac; margin-top: 6px; }}
  @media (max-width: 800px) {{ .charts {{ grid-template-columns: 1fr; }} }}

  .tabs {{ display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid #2a2e3a; }}
  .tab-btn {{ background: none; border: none; color: #9aa0ac; padding: 10px 18px; cursor: pointer;
              font-size: 0.9rem; border-bottom: 2px solid transparent; }}
  .tab-btn.active {{ color: #e6e6e6; border-bottom-color: #5b8def; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
  /* Classe PRÓPRIA, de propósito -- não reaproveita .tab-btn. O troca-de-aba
     escuta TODO elemento .tab-btn e assume que ele tem data-tab; um botão
     comum com essa classe quebrava a troca de aba inteira (achado real,
     ver ARQUITETURA.md seção 11.5). */
  .refresh-btn {{ background: #1a1d27; color: #c7ccd6; border: 1px solid #2a2e3a; border-radius: 6px;
                  padding: 6px 14px; font-size: 0.82rem; cursor: pointer; }}
  .refresh-btn:hover {{ border-color: #5b8def; color: #e6e6e6; }}
  .refresh-btn:disabled {{ opacity: 0.6; cursor: default; }}
  /* Empilhado (não lado a lado): grafo em cima (100% de largura, altura
     em PX fixo vindo de JS -- sizeTreeLayout(), 60% da tela), painel de
     conteúdo embaixo (100% de largura também). Altura de #network é PX
     explícito, nunca %/vh: o vis-network tem redimensionamento
     automático próprio (ResizeObserver) que entra em loop quando o
     container mede o próprio tamanho a partir do conteúdo (canvas pede
     espaço -> container cresce -> canvas mede de novo um espaço maior ->
     pede mais -> nunca converge -- chegou a travar o Chrome de verdade,
     achado real testando). */
  .tree-layout {{ display: flex; flex-direction: column; gap: 16px; }}
  #network {{ width: 100%; min-width: 0; position: relative; overflow: hidden;
              background: #1a1d27; border: 1px solid #2a2e3a; border-radius: 10px; }}
  .side-panel {{ width: 100%; max-height: 340px; background: #1a1d27; border: 1px solid #2a2e3a;
                 border-radius: 10px; padding: 16px; overflow-y: auto; font-size: 0.82rem; }}
  .side-panel h3 {{ margin: 8px 0 4px; font-size: 0.95rem; word-break: break-word; }}
  .side-panel pre {{ white-space: pre-wrap; word-break: break-word; background: #0f1117; padding: 10px;
                      border-radius: 6px; font-size: 0.72rem; max-height: 50vh; overflow-y: auto; }}
  .side-empty {{ color: #9aa0ac; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem;
            color: #0f1117; font-weight: 600; }}
  /* Popup de "Tela cheia" (window.open com o tamanho da tela, ver
     fullscreen-tree-btn abaixo): esconde o cabeçalho/abas (não faz sentido
     lá, a árvore já abre sozinha) e trava o scroll da página inteira --
     sizeTreeLayout() calcula a altura do grafo a partir do espaço que
     realmente sobra, então nunca deveria faltar/sobrar espaço, mas
     overflow:hidden fica de rede de segurança contra arredondamento (achado
     real, 2026-09-05: sem isso, um cálculo levemente errado da altura já
     empurrava a página inteira pra scroll vertical E horizontal). */
  html.fullscreen-mode, body.fullscreen-mode {{ overflow: hidden; }}
  body.fullscreen-mode {{ padding: 12px; }}
  body.fullscreen-mode h1, body.fullscreen-mode .sub, body.fullscreen-mode .tabs {{ display: none; }}
</style>
</head><body>
<h1>codegraph-mcp -- {project_name}</h1>
<div class="sub">Dashboard de indexação + efetividade de uso (gerado ao vivo, dados reais do grafo)</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="overview">Visão geral</button>
  <button class="tab-btn" data-tab="tree">Árvore</button>
  <button class="tab-btn" data-tab="health">Saúde</button>
</div>

<div id="tab-overview" class="tab-content active">
  <div class="grid">
    <div class="card"><div class="n">{coverage['files']}</div><div class="l">arquivos indexados</div></div>
    <div class="card"><div class="n">{coverage['contexts_smart_pct']}%</div><div class="l">contextos com chunking inteligente ({coverage['contexts_smart']}/{coverage['contexts_total']})</div></div>
    <div class="card"><div class="n">{coverage['flows']}</div><div class="l">fluxos mapeados ({coverage['flow_steps']} passos)</div></div>
    <div class="card"><div class="n">{effectiveness['exchanges']}</div><div class="l">trocas de prompt registradas</div></div>
    <div class="card"><div class="n">{effectiveness['with_tools_pct']}%</div><div class="l">trocas que usaram tool do codegraph-mcp</div></div>
    <div class="card"><div class="n">{effectiveness['avg_tokens_per_sec'] or '--'}</div><div class="l">tokens/s médio de geração</div></div>
    <div class="card"><div class="n">~{effectiveness['estimated_tokens_saved']:,}</div><div class="l">tokens poupados (estimativa, ver nota)</div></div>
  </div>

  <div class="charts">
    <div class="chart-box">
      <canvas id="tokensChart"></canvas>
      <div class="note">prompt_tokens / completion_tokens por troca, na ordem em que aconteceram</div>
    </div>
    <div class="chart-box">
      <canvas id="speedChart"></canvas>
      <div class="note">tokens/s de geração por troca -- cai conforme o contexto usado cresce (esperado)</div>
    </div>
  </div>

  <p class="note">"tokens poupados" é uma estimativa, não medição exata: (tamanho médio de
  arquivo inteiro - tamanho médio do trecho que uma tool devolve) × trocas que
  usaram alguma tool do codegraph-mcp, convertido a ~4 caracteres por token.
  Não rastreia byte a byte o que de fato entrou em cada prompt.</p>
</div>

<div id="tab-tree" class="tab-content">
  <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:8px; flex-wrap:wrap;">
    <div id="history-stats" class="note" style="margin-top:0;">carregando estatísticas...</div>
    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
      <span class="note" style="margin-top:0;">teto:</span>
      <input id="max-history-input" type="number" min="1" style="width:90px; background:#0f1117; color:#e6e6e6; border:1px solid #2a2e3a; border-radius:6px; padding:5px 8px; font-size:0.82rem;">
      <span class="note" style="margin-top:0;">MB</span>
      <button id="save-max-history-btn" class="refresh-btn">Salvar teto</button>
      <button id="fullscreen-tree-btn" class="refresh-btn">🖥️ Tela cheia</button>
      <button id="refresh-tree-btn" class="refresh-btn">🔄 Atualizar árvore</button>
    </div>
  </div>
  <div class="tree-layout">
    <div id="network"></div>
    <div id="side-panel" class="side-panel"><div class="side-empty">Clique num nó pra ver o conteúdo. Nós já
      abrem sozinhos os filhos na primeira vez que você clica -- clique nos filhos novos
      pra continuar descendo.</div></div>
  </div>
  <div class="note" id="tree-footer-note">Arraste pra navegar, roda do mouse pra zoom. Linha sólida = hierarquia
  (arquivo→trecho, fluxo→passo); linha tracejada laranja = referência cruzada
  (passo de fluxo → trecho de código que implementa).</div>
</div>

<div id="tab-health" class="tab-content">
  <div style="display:flex; justify-content:flex-end; margin-bottom:8px;">
    <button id="refresh-health-btn" class="refresh-btn">🔄 Atualizar saúde</button>
  </div>
  <div id="health-grid" class="grid"><div class="note">carregando...</div></div>
</div>

<script>
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'tree' && !window.__treeInited) {{
      window.__treeInited = true;
      initTree();
    }}
    if (btn.dataset.tab === 'health') loadHealth();
  }});
}});

function formatBytesShort(bytes) {{
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
}}

function healthCard(label, value, ok) {{
  const color = ok === true ? '#3ecf8e' : ok === false ? '#e05b5b' : '#9aa0ac';
  return '<div class="card"><div class="n" style="color:' + color + '; font-size:1.1rem;">' + value + '</div><div class="l">' + label + '</div></div>';
}}

async function loadHealth() {{
  const grid = document.getElementById('health-grid');
  grid.innerHTML = '<div class="note">carregando...</div>';
  try {{
    const res = await fetch('/api/health' + PROJECT_Q);
    const data = await res.json();
    const cards = [];
    cards.push(healthCard('proxy (esta página)', 'no ar', true));
    const upOk = data.upstream.status === 'ok';
    cards.push(healthCard('modelo (llama-server)', data.upstream.status, upOk));
    cards.push(healthCard('projeto ativo', data.active_project ? data.active_project.split('/').pop() : '(nenhum)', data.active_project ? true : null));
    if (data.db.exists) {{
      cards.push(healthCard('banco de dados', formatBytesShort(data.db.size_bytes), true));
      const c = data.db.counts;
      cards.push(healthCard('nós indexados', (c.file + c.file_context + c.flow + c.flow_step) + ' código + ' + c.history + ' histórico', true));
    }} else {{
      cards.push(healthCard('banco de dados', 'não encontrado', false));
    }}
    cards.push(healthCard('reindexação automática', data.reindexing ? 'rodando agora' : 'ociosa (em dia)', data.reindexing ? null : true));
    grid.innerHTML = cards.join('');
  }} catch (e) {{
    grid.innerHTML = '<div class="note">erro checando saúde: ' + escapeHtml(String(e)) + '</div>';
  }}
}}

document.getElementById('refresh-health-btn').addEventListener('click', loadHealth);

const labels = {json.dumps(labels)};
new Chart(document.getElementById('tokensChart'), {{
  type: 'line',
  data: {{ labels, datasets: [
    {{ label: 'prompt_tokens', data: {json.dumps(prompt_tok)}, borderColor: '#5b8def', tension: 0.2 }},
    {{ label: 'completion_tokens', data: {json.dumps(completion_tok)}, borderColor: '#e0a458', tension: 0.2 }},
  ]}},
  options: {{ responsive: true, scales: {{ x: {{ display: false }} }} }}
}});
new Chart(document.getElementById('speedChart'), {{
  type: 'line',
  data: {{ labels, datasets: [
    {{ label: 'tokens/s', data: {json.dumps(speed)}, borderColor: '#5bc99a', tension: 0.2 }},
  ]}},
  options: {{ responsive: true, scales: {{ x: {{ display: false }} }} }}
}});

// ---- aba Árvore ----
const PROJECT_Q = {json.dumps(project_query)};
const IS_FULLSCREEN = new URLSearchParams(location.search).has('fullscreen');
const TYPE_STYLE = {{
  file: {{ color: '#5b8def', shape: 'box' }},
  file_context: {{ color: '#8fb8f6', shape: 'dot' }},
  flow: {{ color: '#e0a458', shape: 'box' }},
  flow_step: {{ color: '#f0c98a', shape: 'dot' }},
  history: {{ color: '#b18cf0', shape: 'dot' }},
}};
const HISTORY_BUCKET_ID = 'hist:recent';
const HISTORY_MORE_ID = 'hist:more';
let visNodes, visEdges, network;
const expanded = new Set();

function qs(extra) {{
  return PROJECT_Q + (PROJECT_Q ? '&' : '?') + extra;
}}

function nodeToVis(n) {{
  const style = TYPE_STYLE[n.type] || {{ color: '#999', shape: 'dot' }};
  // Rótulo de histórico: preview de texto (até 80 caracteres, ver
  // history.py) nunca coube do lado de dezenas de irmãos sem sobrepor --
  // truncar em 18 caracteres ainda estourava a largura de sobra (achado
  // real, visto na tela do usuário -- textos parecidos/repetidos ficam
  // uma parede ilegível mesmo curtos). Troca de estratégia: rótulo vira
  // só o número do nó (`#id`) -- largura fixa e pequena, nunca colide,
  // e o `id` já É a ordem real de entrada (autoincrement do SQLite =
  // ordem cronológica de criação, não precisa contador à parte).
  // Descrição completa só no hover (title) -- nunca no rótulo.
  const label = n.type === 'history' ? ('#' + n.id) : n.name;
  return {{ id: n.id, label, shape: style.shape, color: style.color,
            title: n.type + (n.path ? ' · ' + n.path : '') + (n.type === 'history' ? '\\n' + n.name : '') }};
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]);
}}

function renderSidePanel(node) {{
  const panel = document.getElementById('side-panel');
  const style = TYPE_STYLE[node.type] || {{ color: '#555' }};
  const body = node.content
    ? '<pre>' + escapeHtml(node.content.slice(0, 4000)) + (node.content.length > 4000 ? '\\n... (cortado, ' + node.content.length + ' caracteres no total)' : '') + '</pre>'
    : '<div class="side-empty">(sem conteúdo direto -- veja os filhos na árvore)</div>';
  const loc = [node.path, node.start_line ? ('linhas ' + node.start_line + '-' + node.end_line) : null]
    .filter(Boolean).join(' · ');
  panel.innerHTML = '<span class="badge" style="background:' + style.color + '">' + node.type + '</span>'
    + '<h3>' + escapeHtml(node.name) + '</h3>'
    + (loc ? '<div class="note">' + escapeHtml(loc) + '</div>' : '')
    + body;
}}

// Arquivos são agrupados em pastas (client-side, a partir do path -- sem
// chamada nova ao servidor) porque um projeto real tem centenas de
// arquivos-raiz; jogar tudo como filho direto de "root" de uma vez deixa
// o grafo largo demais pra caber na tela (achado testando de verdade
// contra 412 arquivos: a árvore renderizava, só ficava fora da área
// visível). Pastas são nós sintéticos (id "dir:caminho/da/pasta"), só
// existem no navegador -- não tem endpoint novo pra elas.
let folderTree = null;

function buildFolderTree(fileNodes) {{
  const root = {{ children: {{}}, files: [] }};
  for (const f of fileNodes) {{
    const parts = f.path.split('/');
    const fileName = parts.pop();
    let cur = root, prefix = '';
    for (const part of parts) {{
      prefix = prefix ? prefix + '/' + part : part;
      if (!cur.children[part]) cur.children[part] = {{ fullPath: prefix, children: {{}}, files: [] }};
      cur = cur.children[part];
    }}
    cur.files.push(f);
  }}
  return root;
}}

function findDirNode(fullPath) {{
  if (!fullPath) return folderTree;
  let cur = folderTree;
  for (const part of fullPath.split('/')) {{
    cur = cur && cur.children[part];
  }}
  return cur || null;
}}

function dirVisNode(name, fullPath) {{
  return {{ id: 'dir:' + fullPath, label: '\\uD83D\\uDCC1 ' + name, shape: 'box', color: '#4a4e5a' }};
}}

// Desloca a câmera pro centro dos nós recém-adicionados SEM mudar o zoom --
// network.fit() sempre recalcula o zoom pra caber os nós dados, o que
// desfazia qualquer zoom manual que a pessoa já tivesse ajustado (achado
// real, 2026-09-05: usuário deu zoom, clicou pra abrir um nó com filhos, e
// o zoom voltava sozinho pro nível "de encaixe" -- não é o que foi pedido,
// o pedido era manter o zoom escolhido e só trazer o novo pra vista). Usa
// moveTo com a escala ATUAL (network.getScale()), só troca a posição.
function focusNewNodes(nodeIds) {{
  if (!nodeIds.length) return;
  const positions = network.getPositions(nodeIds);
  const pts = Object.values(positions);
  if (!pts.length) return;
  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
  network.moveTo({{ position: {{ x: cx, y: cy }}, scale: network.getScale(), animation: {{ duration: 400, easingFunction: 'easeInOutQuad' }} }});
}}

// Paginado por cursor (before_id) -- mais recentes primeiro. Cada página
// pendura um nó sentinela "carregar mais antigas" no fim; clicar nele
// remove o sentinela antigo e busca a próxima leva a partir do cursor
// guardado nele (visNodes.get(HISTORY_MORE_ID).cursor). Sem isso, projetos
// com muita conversa acumulada jogavam tudo de uma vez -- dezenas de nós
// numa fileira só (achado real, 2026-09-05, ver ARQUITETURA.md).
async function loadHistoryPage(parentId, beforeId) {{
  const q = beforeId ? qs('before_id=' + beforeId) : PROJECT_Q;
  const res = await fetch('/api/tree/history' + q);
  const data = await res.json();
  const newNodes = [], newEdges = [];
  for (const h of (data.nodes || [])) {{
    if (!visNodes.get(h.id)) newNodes.push(nodeToVis(h));
    newEdges.push({{ from: parentId, to: h.id, color: {{ color: '#3a3f4d' }} }});
  }}
  if (data.has_more && data.next_before_id) {{
    newNodes.push({{
      id: HISTORY_MORE_ID, label: '\\u2026 carregar mais antigas', shape: 'box',
      color: {{ background: '#262b38', border: '#4a4e5a' }}, font: {{ color: '#9aa0ac' }},
      cursor: data.next_before_id,
    }});
    newEdges.push({{ from: parentId, to: HISTORY_MORE_ID, color: {{ color: '#3a3f4d' }}, dashes: true }});
  }}
  if (newNodes.length) visNodes.add(newNodes);
  if (newEdges.length) visEdges.add(newEdges);
  focusNewNodes(newNodes.map(n => n.id));
}}

async function onNodeClick(params) {{
  if (!params.nodes.length) return;
  const id = params.nodes[0];
  if (id === 'root') return;

  if (typeof id === 'string' && id.startsWith('dir:')) {{
    if (expanded.has(id)) return;  // pasta: filhos não mudam, nada novo pra buscar
    expanded.add(id);
    const fullPath = id.slice(4);
    const dirNode = findDirNode(fullPath);
    if (!dirNode) return;

    document.getElementById('side-panel').innerHTML =
      '<span class="badge" style="background:#4a4e5a">pasta</span><h3>' + escapeHtml(fullPath) + '</h3>'
      + '<div class="note">' + Object.keys(dirNode.children).length + ' subpasta(s), ' + dirNode.files.length + ' arquivo(s)</div>';

    const newNodes = [], newEdges = [];
    for (const key of Object.keys(dirNode.children)) {{
      const sub = dirNode.children[key];
      if (!visNodes.get('dir:' + sub.fullPath)) newNodes.push(dirVisNode(key, sub.fullPath));
      newEdges.push({{ from: id, to: 'dir:' + sub.fullPath, color: {{ color: '#3a3f4d' }} }});
    }}
    for (const f of dirNode.files) {{
      if (!visNodes.get(f.id)) newNodes.push(nodeToVis(f));
      newEdges.push({{ from: id, to: f.id, color: {{ color: '#3a3f4d' }} }});
    }}
    if (newNodes.length) visNodes.add(newNodes);
    if (newEdges.length) visEdges.add(newEdges);
    focusNewNodes(newNodes.map(n => n.id));
    return;
  }}

  if (id === HISTORY_BUCKET_ID) {{
    if (expanded.has(id)) return;  // primeira página já carregada, "atualizar árvore" recarrega tudo
    expanded.add(id);
    document.getElementById('side-panel').innerHTML =
      '<span class="badge" style="background:' + TYPE_STYLE.history.color + '">histórico</span><h3>Memórias de conversa</h3>'
      + '<div class="note">Prompt + resposta de trocas reais com o modelo, guardadas automaticamente pelo proxy. Mais recentes primeiro -- clique em "carregar mais antigas" pra ver o resto.</div>';
    await loadHistoryPage(HISTORY_BUCKET_ID, null);
    return;
  }}

  if (id === HISTORY_MORE_ID) {{
    const sentinel = visNodes.get(HISTORY_MORE_ID);
    if (!sentinel) return;  // clique duplo -- já removido pela primeira chamada
    visNodes.remove(HISTORY_MORE_ID);
    await loadHistoryPage(HISTORY_BUCKET_ID, sentinel.cursor);
    return;
  }}

  // SEMPRE busca e atualiza o painel de conteúdo, mesmo pra nó já visitado
  // antes -- bug real, 2026-09-05: `expanded` também bloqueava isso, então
  // reclicar num nó (ex: histórico) já aberto antes deixava o painel
  // travado mostrando o conteúdo do penúltimo nó clicado, nunca o do atual
  // (usuário reparou trocando entre vários nós de histórico em sequência).
  // `expanded` só deve controlar "já busquei filhos/relações" (evita
  // duplicar na árvore), nunca o conteúdo do painel.
  const res = await fetch('/api/tree/node/' + id + PROJECT_Q);
  const data = await res.json();
  if (data.error) return;
  renderSidePanel(data.node);
  if (expanded.has(id)) return;
  expanded.add(id);

  const newNodes = [];
  const newEdges = [];
  for (const c of data.children) {{
    if (!visNodes.get(c.id)) newNodes.push(nodeToVis(c));
    newEdges.push({{ from: id, to: c.id, color: {{ color: '#3a3f4d' }} }});
  }}
  for (const r of data.related_out) {{
    if (!visNodes.get(r.target.id)) newNodes.push(nodeToVis(r.target));
    newEdges.push({{ from: id, to: r.target.id, dashes: true, color: {{ color: '#e0a458' }}, label: r.type, font: {{ color: '#e0a458', size: 9, background: '#0f1117' }} }});
  }}
  for (const r of data.related_in) {{
    if (!visNodes.get(r.source.id)) newNodes.push(nodeToVis(r.source));
    newEdges.push({{ from: r.source.id, to: id, dashes: true, color: {{ color: '#e0a458' }}, label: r.type, font: {{ color: '#e0a458', size: 9, background: '#0f1117' }} }});
  }}
  if (newNodes.length) visNodes.add(newNodes);
  if (newEdges.length) visEdges.add(newEdges);
  if (newNodes.length) focusNewNodes(newNodes.map(n => n.id));
}}

function sizeTreeLayout() {{
  // Altura do GRAFO calculada a partir do espaço que REALMENTE sobra na
  // janela -- nunca a partir do que o vis-network reporta precisar (isso
  // é o que causava o loop de resize, ver nota no CSS) e nunca como uma
  // fração "no chute" da tela (era assim antes: 85% fixo no popup de tela
  // cheia -- somado ao cabeçalho/painel/rodapé que ainda apareciam,
  // estourava a altura da janela e a página inteira ganhava barra de
  // rolagem vertical E horizontal, porque a barra vertical rouba largura
  // útil. Achado real, 2026-09-05, print do usuário mostrando as duas
  // barras no popup). Agora: mede a posição real de onde o grafo começa,
  // subtrai o que vem depois dele (painel de conteúdo, no tamanho MÁXIMO
  // que ele pode ter -- ver o max-height do `.side-panel` no CSS -- e o
  // rodapé de instruções) e usa o que sobrar.
  const network = document.getElementById('network');
  const layout = document.querySelector('.tree-layout');
  const footer = document.getElementById('tree-footer-note');
  const panelMaxHeight = 340; // deve bater com .side-panel{{max-height}} no CSS
  const gap = 16; // .tree-layout{{gap}} -- 1 gap entre #network e o painel
  const bottomMargin = 12;
  const available = window.innerHeight - layout.getBoundingClientRect().top
    - panelMaxHeight - footer.getBoundingClientRect().height - gap - bottomMargin;
  network.style.height = Math.max(280, available) + 'px';
}}

function setupResizeListener() {{
  if (window.__resizeListenerAdded) return;
  window.__resizeListenerAdded = true;
  let resizeTimer = null;
  window.addEventListener('resize', () => {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {{
      sizeTreeLayout();
      if (network) network.redraw();
    }}, 150);
  }});
}}

async function buildTree() {{
  expanded.clear();
  if (network) {{ network.destroy(); network = null; }}

  const res = await fetch('/api/tree/roots' + PROJECT_Q);
  const data = await res.json();
  const allNodes = data.nodes || [];
  const fileNodes = allNodes.filter(n => n.type === 'file');
  const flowNodes = allNodes.filter(n => n.type === 'flow');
  folderTree = buildFolderTree(fileNodes);

  const nodeItems = [
    {{ id: 'root', label: data.project || 'projeto', shape: 'ellipse', color: '#2a2e3a', font: {{ color: '#e6e6e6' }} }},
    {{ id: HISTORY_BUCKET_ID, label: '🕒 Histórico (memória)', shape: 'box', color: TYPE_STYLE.history.color }},
  ];
  const edgeItems = [{{ from: 'root', to: HISTORY_BUCKET_ID, color: {{ color: '#3a3f4d' }} }}];
  for (const key of Object.keys(folderTree.children)) {{
    const dir = folderTree.children[key];
    nodeItems.push(dirVisNode(key, dir.fullPath));
    edgeItems.push({{ from: 'root', to: 'dir:' + dir.fullPath, color: {{ color: '#3a3f4d' }} }});
  }}
  for (const f of folderTree.files) {{
    nodeItems.push(nodeToVis(f));
    edgeItems.push({{ from: 'root', to: f.id, color: {{ color: '#3a3f4d' }} }});
  }}
  for (const fl of flowNodes) {{
    nodeItems.push(nodeToVis(fl));
    edgeItems.push({{ from: 'root', to: fl.id, color: {{ color: '#3a3f4d' }} }});
  }}

  visNodes = new vis.DataSet(nodeItems);
  visEdges = new vis.DataSet(edgeItems);
  network = new vis.Network(document.getElementById('network'), {{ nodes: visNodes, edges: visEdges }}, {{
    // autoResize (default da biblioteca, nao desligado aqui) cuida do
    // zoom/redesenho certo -- so' funciona sem loop porque #network tem
    // altura fixa em px vinda de fora (sizeTreeLayout()), nao porque
    // desligamos o recurso da biblioteca.
    nodes: {{ size: 10, font: {{ size: 11, color: '#c7ccd6' }} }},  // bolas menores (default da lib e 25)
    // nodeSpacing/levelSeparation maiores que o default da lib -- com pouco
    // espaço, caixas com rótulo comprido (ex: nome de arquivo longo)
    // desenham por cima da vizinha mesmo a árvore crescendo certo pra baixo
    // (achado real, 2026-09-05, print do usuário). direction 'UD' já cresce
    // de cima pra baixo -- root no topo, cada nível novo abaixo do anterior.
    layout: {{ hierarchical: {{ direction: 'UD', sortMethod: 'directed', nodeSpacing: 160, levelSeparation: 150, blockShifting: true, edgeMinimization: true }} }},
    physics: false,
    interaction: {{ hover: true, dragNodes: true }},
    edges: {{ arrows: 'to', smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }} }},
  }});
  network.on('click', onNodeClick);
  network.once('afterDrawing', () => network.fit());
}}

function formatSize(bytes) {{
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
}}

async function loadHistoryStats() {{
  const el = document.getElementById('history-stats');
  try {{
    const res = await fetch('/api/history-config' + PROJECT_Q);
    const data = await res.json();
    if (data.error) {{ el.textContent = data.error; return; }}
    el.textContent = 'Grafo: ' + formatSize(data.db_size_bytes) + ' de ' +
      formatSize(data.max_history_mb * 1024 * 1024) + ' (' + data.percent_used + '% do teto de histórico)';
    document.getElementById('max-history-input').value = data.max_history_mb;
  }} catch (e) {{
    el.textContent = 'não consegui carregar a estatística';
  }}
}}

async function saveMaxHistory() {{
  const input = document.getElementById('max-history-input');
  const btn = document.getElementById('save-max-history-btn');
  const mb = parseInt(input.value, 10);
  if (!mb || mb <= 0) {{ alert('digite um número de MB válido, maior que zero'); return; }}

  btn.disabled = true;
  try {{
    const res = await fetch('/api/history-config' + PROJECT_Q, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ max_history_mb: mb }}),
    }});
    const data = await res.json();
    if (!res.ok || data.error) {{
      // Segurança: teto novo menor que o banco já ocupa hoje -- o backend
      // recusa (history.LimitTooSmallError), a ação NÃO é aplicada.
      alert('Não deu pra salvar:\\n\\n' + data.error);
      return;
    }}
    await loadHistoryStats();
  }} finally {{
    btn.disabled = false;
  }}
}}

document.getElementById('save-max-history-btn').addEventListener('click', saveMaxHistory);

async function initTree() {{
  sizeTreeLayout();
  setupResizeListener();
  await Promise.all([buildTree(), loadHistoryStats()]);
}}

async function refreshTree() {{
  const btn = document.getElementById('refresh-tree-btn');
  btn.disabled = true;
  btn.textContent = '🔄 atualizando...';
  try {{
    sizeTreeLayout();
    await Promise.all([buildTree(), loadHistoryStats()]);
    document.getElementById('side-panel').innerHTML =
      '<div class="side-empty">Árvore atualizada. Clique num nó pra ver o conteúdo.</div>';
  }} finally {{
    btn.disabled = false;
    btn.textContent = '🔄 Atualizar árvore';
  }}
}}

document.getElementById('refresh-tree-btn').addEventListener('click', refreshTree);

document.getElementById('fullscreen-tree-btn').addEventListener('click', () => {{
  const url = new URL(window.location.href);
  url.searchParams.set('fullscreen', '1');
  // "janela mesmo" (nao aba) -- popup com o tamanho da tela inteira.
  window.open(
    url.toString(), '_blank',
    'width=' + screen.availWidth + ',height=' + screen.availHeight + ',left=0,top=0'
  );
}});

if (IS_FULLSCREEN) {{
  // Pagina aberta so' pra ver a arvore -- ja abre direto nela, sem
  // precisar clicar na aba. Classe tem que entrar ANTES do click (que
  // dispara initTree -> sizeTreeLayout): e' ela que esconde h1/sub/abas,
  // e sizeTreeLayout mede a posicao do ".tree-layout" DEPOIS de esconder
  // isso, senao mede um espaco que nao existe mais.
  document.documentElement.classList.add('fullscreen-mode');
  document.body.classList.add('fullscreen-mode');
  document.querySelector('[data-tab="tree"]').click();
}}
</script>
</body></html>"""


async def dashboard(request: Request):
    project_param = request.query_params.get("project")
    project_dir = Path(project_param) if project_param else state.get_active_project()
    if project_dir is None:
        return Response("nenhum projeto ativo -- rode `codegraph setup <projeto>` primeiro", status_code=404)
    db_path = project_dir / ".codegraph" / "graph.db"
    if not db_path.exists():
        return Response(f"grafo não encontrado em {db_path}", status_code=404)

    conn = db.connect(str(db_path))
    try:
        coverage = metrics.coverage_stats(conn)
        effectiveness = metrics.effectiveness_summary(conn)
        hist = metrics.history_rows(conn)
    finally:
        conn.close()

    project_query = f"?project={project_param}" if project_param else ""
    html = _render_dashboard(project_dir.name, project_query, coverage, effectiveness, hist)
    return Response(html, media_type="text/html")


async def health(request: Request):
    """Devolve 200 sozinho, sem depender do llama-server upstream estar de
    pé. Antes disso, um script que checava `/health` (ex: alias
    `llama-qwen` do usuário, ver KIMI_CONFIG.md) caía no catch-all
    (`passthrough`) e recebia um 500 de tentar repassar pro upstream (que
    ainda não tinha subido) -- "funcionava" só porque `curl` sem `-f`
    trata qualquer resposta HTTP, mesmo erro, como sucesso. Achado real,
    2026-09-05: frágil, escondia que não existia health check de verdade."""
    return JSONResponse({"ok": True})


async def passthrough(request: Request):
    url = f"{UPSTREAM}{request.url.path}"
    body = await request.body()
    resp = await client.request(
        request.method, url, content=body, params=request.query_params,
        headers={
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        },
    )
    return Response(
        resp.content, status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/dashboard", dashboard, methods=["GET"]),
    Route("/api/tree/roots", api_tree_roots, methods=["GET"]),
    Route("/api/tree/history", api_tree_history, methods=["GET"]),
    Route("/api/tree/node/{node_id:int}", api_tree_node, methods=["GET"]),
    Route("/api/history-config", api_history_config_get, methods=["GET"]),
    Route("/api/history-config", api_history_config_set, methods=["POST"]),
    Route("/api/health", api_health, methods=["GET"]),
    Route("/{path:path}", passthrough, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]),
])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT)
