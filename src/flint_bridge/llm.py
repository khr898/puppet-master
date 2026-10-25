import re
import time
from dataclasses import dataclass, field

import httpx

from flint_bridge.config import Config
from flint_bridge.report import est_tokens

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class FlintOffline(Exception):
    """The model server is unreachable, timed out, or returned an unusable response."""


@dataclass
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    reasoning_tokens: int = 0
    raw: str = field(default="", compare=False, repr=False)  # the reply as sent, thinking included


def _strip_think(content: str) -> tuple[str, str]:
    """Return (content without <think> blocks, the removed thinking text)."""
    removed = "".join(_THINK.findall(content))
    return _THINK.sub("", content).strip(), removed


async def complete(
    cfg: Config, system: str, user: str, *, max_tokens: int, schema: dict | None = None
) -> Completion:
    payload: dict = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": cfg.temperature,
        "max_tokens": max_tokens,
        "cache_prompt": True,
    }
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
            resp = await client.post(f"{cfg.base_url}/v1/chat/completions", json=payload)
            resp.raise_for_status()
        body = resp.json()
        message = body["choices"][0]["message"]
        content = message["content"] or ""
    except (httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, IndexError) as e:
        raise FlintOffline(str(e) or type(e).__name__) from e

    text, removed = _strip_think(content)
    usage = body.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning is None:
        reasoning = est_tokens(removed + (message.get("reasoning_content") or ""))
    return Completion(
        text=text,
        tokens_in=usage.get("prompt_tokens") or est_tokens(system + user),
        tokens_out=usage.get("completion_tokens") or est_tokens(content),
        reasoning_tokens=reasoning,
        raw=f"<think>{message['reasoning_content']}</think>{content}"
        if message.get("reasoning_content")
        else content,
    )


async def health(cfg: Config) -> dict:
    """GET /health and /props. Returns {up, latency_ms, ctx}, or {up: False} on any failure.

    A server without llama.cpp's /health (Ollama, LM Studio, mlx_lm) is up if /v1/models
    answers; a 503 from /health means llama-server is still loading, so that stays down.
    """
    try:
        async with httpx.AsyncClient(timeout=min(cfg.timeout_s, 5)) as client:
            start = time.perf_counter()
            resp = await client.get(f"{cfg.base_url}/health")
            if resp.status_code == 404:
                resp = await client.get(f"{cfg.base_url}/v1/models")
            resp.raise_for_status()
            latency_ms = round((time.perf_counter() - start) * 1000)
            ctx = cfg.ctx
            try:
                props = (await client.get(f"{cfg.base_url}/props")).json()
                ctx = props["default_generation_settings"]["n_ctx"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
    except (httpx.HTTPError, ValueError):
        return {"up": False}
    return {"up": True, "latency_ms": latency_ms, "ctx": ctx}
