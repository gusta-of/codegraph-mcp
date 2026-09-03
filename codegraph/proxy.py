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
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from codegraph import db, history, metrics, state

UPSTREAM = os.environ.get("CODEGRAPH_UPSTREAM", "http://127.0.0.1:8080")
LISTEN_PORT = int(os.environ.get("CODEGRAPH_PROXY_PORT", "8081"))

client = httpx.AsyncClient(timeout=None)

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


def _last_user_message(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            return "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return content or ""
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
) -> None:
    if not prompt and not content:
        return
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
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            used_tools = {
                tc["function"]["name"] for tc in (msg.get("tool_calls") or [])
                if _is_codegraph_tool(tc.get("function", {}).get("name") or "")
            }
            usage = data.get("usage")
        except (KeyError, IndexError, ValueError):
            pass
        asyncio.create_task(_log_exchange(prompt, content, reasoning, usage, None, used_tools))
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

    async def gen():
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
                    for name in tools:
                        if _is_codegraph_tool(name):
                            used_tools.add(name)
        await upstream_resp.aclose()
        asyncio.create_task(_log_exchange(
            prompt, "".join(content_parts), "".join(reasoning_parts),
            usage_holder or None, timings_holder or None, used_tools,
        ))

    return StreamingResponse(gen(), media_type="text/event-stream")


def _render_dashboard(project_name: str, coverage: dict, effectiveness: dict, hist: list[dict]) -> str:
    labels = [h["created_at"] for h in hist]
    prompt_tok = [h.get("prompt_tokens") for h in hist]
    completion_tok = [h.get("completion_tokens") for h in hist]
    speed = [h.get("predicted_per_second") for h in hist]

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>codegraph-mcp -- {project_name}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f1117; color: #e6e6e6; margin: 0; padding: 24px; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
  .sub {{ color: #9aa0ac; margin-bottom: 24px; font-size: 0.85rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 28px; }}
  .card {{ background: #1a1d27; border: 1px solid #2a2e3a; border-radius: 10px; padding: 14px 16px; }}
  .card .n {{ font-size: 1.6rem; font-weight: 600; }}
  .card .l {{ font-size: 0.78rem; color: #9aa0ac; margin-top: 2px; }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .chart-box {{ background: #1a1d27; border: 1px solid #2a2e3a; border-radius: 10px; padding: 16px; }}
  .note {{ font-size: 0.75rem; color: #9aa0ac; margin-top: 6px; }}
  @media (max-width: 800px) {{ .charts {{ grid-template-columns: 1fr; }} }}
</style>
</head><body>
<h1>codegraph-mcp -- {project_name}</h1>
<div class="sub">Dashboard de indexação + efetividade de uso (gerado ao vivo, dados reais do grafo)</div>

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

<script>
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

    html = _render_dashboard(project_dir.name, coverage, effectiveness, hist)
    return Response(html, media_type="text/html")


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
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/dashboard", dashboard, methods=["GET"]),
    Route("/{path:path}", passthrough, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]),
])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT)
