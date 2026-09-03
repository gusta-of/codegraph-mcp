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

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from codegraph import db, history, state

UPSTREAM = os.environ.get("CODEGRAPH_UPSTREAM", "http://127.0.0.1:8080")
LISTEN_PORT = int(os.environ.get("CODEGRAPH_PROXY_PORT", "8081"))

client = httpx.AsyncClient(timeout=None)


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


def _extract_delta_text(chunk: dict) -> tuple[str, str]:
    try:
        delta = chunk["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return "", ""
    return delta.get("content") or "", delta.get("reasoning_content") or ""


async def _log_exchange(prompt: str, content: str, reasoning: str) -> None:
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
        history.record_exchange(
            conn, str(db_path), prompt=prompt, response=content,
            max_bytes=max_bytes, metadata={"reasoning_chars": len(reasoning)},
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
        content, reasoning = "", ""
        try:
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
        except (KeyError, IndexError, ValueError):
            pass
        asyncio.create_task(_log_exchange(prompt, content, reasoning))
        return Response(
            resp.content, status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    upstream_req = client.build_request(
        "POST", f"{UPSTREAM}/v1/chat/completions", content=body,
        headers={"content-type": "application/json"},
    )
    upstream_resp = await client.send(upstream_req, stream=True)
    content_parts: list[str] = []
    reasoning_parts: list[str] = []

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
                    c, r = _extract_delta_text(chunk)
                    if c:
                        content_parts.append(c)
                    if r:
                        reasoning_parts.append(r)
        await upstream_resp.aclose()
        asyncio.create_task(
            _log_exchange(prompt, "".join(content_parts), "".join(reasoning_parts))
        )

    return StreamingResponse(gen(), media_type="text/event-stream")


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
    Route("/{path:path}", passthrough, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]),
])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT)
