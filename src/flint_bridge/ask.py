from collections.abc import Awaitable, Callable

from flint_bridge.budget import fits
from flint_bridge.config import Config
from flint_bridge.llm import complete
from flint_bridge.read import Usage
from flint_bridge.report import est_tokens

ASK_MAX_TOKENS = 1500

SYSTEM_ASK = "Write the requested draft. Be concise."


async def flint_ask_impl(
    cfg: Config,
    prompt: str,
    context: str = "",
    *,
    usage: Usage,
    ready: Callable[[], Awaitable[dict | None]] | None = None,
) -> dict:
    """Draft prose with Flint. The result is never verified, and says so."""
    # Context first, the request last, like the read and write prompts (KV cache reuse).
    user = f"{context}\n\n{prompt}" if context else prompt
    max_out = ASK_MAX_TOKENS + cfg.reasoning_reserve
    tokens = est_tokens(SYSTEM_ASK) + est_tokens(user)
    if not fits(tokens, max_out, cfg):
        return {
            "error": "over_budget",
            "est_tokens": tokens,
            "limit": cfg.ctx - max_out,
            "hint": "shorten the prompt or context",
        }
    if ready is not None and (err := await ready()) is not None:
        return err
    reply = await complete(cfg, SYSTEM_ASK, user, max_tokens=max_out)
    usage.tokens_in += reply.tokens_in
    usage.tokens_out += reply.tokens_out
    usage.reasoning_tokens += reply.reasoning_tokens
    return {"draft": reply.text, "verified": False}
