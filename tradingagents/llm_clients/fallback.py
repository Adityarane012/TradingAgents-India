"""Fall back to another model when the configured one is overloaded.

Free-tier Gemini models go through demand spikes and answer every call with
503 UNAVAILABLE, "This model is currently experiencing high demand". That is
not a quota problem and not a code problem: the same key works on a different
model seconds later. It has cost this project real runs — two stocks on
2026-09-21, and the whole 19:00 batch on 2026-09-24, where every call to
gemini-3.1-flash-lite failed while gemini-2.5-flash answered normally.

So a model can be given understudies. Each call tries the primary first and
moves down the list only on a transient provider failure. A bad request, a
missing key or an exhausted daily quota is raised immediately: retrying those
on another model wastes its quota too.

The wrapper is a Runnable, and forwards ``bind_tools`` and
``with_structured_output`` to every model in the list, so the analysts (which
bind tools) and the sentiment analyst (which asks for structured output) keep
their fallbacks. Without fallbacks configured nothing here is used: the
primary model is returned untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.runnables import Runnable

logger = logging.getLogger(__name__)

# Provider-side "come back later": the model is up but swamped. Quota
# exhaustion (RESOURCE_EXHAUSTED / "PerDay") is deliberately not here — the
# day's budget is spent, and a fallback would only spend another model's.
_TRANSIENT = re.compile(
    r"\b(503|502|504|UNAVAILABLE|overloaded|high demand|temporarily unavailable|"
    r"internal server error|deadline exceeded|timeout)\b",
    re.IGNORECASE,
)
_NOT_TRANSIENT = re.compile(r"RESOURCE_EXHAUSTED|PerDay|quota|API key|permission|"
                            r"not found|invalid", re.IGNORECASE)


def is_transient(exc: BaseException) -> bool:
    """Whether another model is worth trying for this failure."""
    text = f"{type(exc).__name__}: {exc}"
    if _NOT_TRANSIENT.search(text):
        return False
    return bool(_TRANSIENT.search(text))


class FallbackChatModel(Runnable):
    """Tries each model in turn, moving on only for transient failures."""

    def __init__(self, models: list[Any], names: list[str] | None = None):
        if not models:
            raise ValueError("FallbackChatModel needs at least one model")
        self.models = models
        self.names = names or [getattr(m, "model", "?") for m in models]

    # -- Runnable ---------------------------------------------------------
    def invoke(self, input, config=None, **kwargs):
        last: BaseException | None = None
        for index, model in enumerate(self.models):
            try:
                return model.invoke(input, config, **kwargs)
            except Exception as exc:  # noqa: BLE001 — re-raised below
                last = exc
                remaining = index < len(self.models) - 1
                if not (remaining and is_transient(exc)):
                    raise
                logger.warning("%s unavailable (%s); falling back to %s",
                               self.names[index], type(exc).__name__, self.names[index + 1])
        raise last  # unreachable while models is non-empty

    # -- Chat-model surface the agents rely on ----------------------------
    def bind_tools(self, *args, **kwargs):
        return FallbackChatModel([m.bind_tools(*args, **kwargs) for m in self.models],
                                 self.names)

    def with_structured_output(self, *args, **kwargs):
        return FallbackChatModel([m.with_structured_output(*args, **kwargs) for m in self.models],
                                 self.names)

    def __getattr__(self, name):
        # Anything else (model_name, temperature, ...) comes from the primary.
        return getattr(self.models[0], name)


def with_model_fallbacks(primary: Any, build: Any, models: list[str], primary_name: str = "?"):
    """Wrap ``primary`` so it falls back to each model in ``models``.

    ``build`` takes a model name and returns a chat model. A model that cannot
    be built (unknown name, missing key) is skipped with a warning rather than
    failing the run — a broken understudy must not stop the primary working.
    """
    if not models:
        return primary
    chain, names = [primary], [primary_name]
    for name in models:
        if name == primary_name:
            continue
        try:
            chain.append(build(name))
            names.append(name)
        except Exception as exc:  # noqa: BLE001 — an unusable fallback is just skipped
            logger.warning("fallback model %r unavailable: %s", name, exc)
    if len(chain) == 1:
        return primary
    return FallbackChatModel(chain, names)
