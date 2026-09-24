"""Tests for falling back to another model when one is overloaded.

The case these come from: on 2026-09-24 every call to gemini-3.1-flash-lite
returned 503 "high demand" for over an hour, failing the whole 19:00 batch,
while gemini-2.5-flash answered the same key normally.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from tradingagents.llm_clients.fallback import (
    FallbackChatModel,
    is_transient,
    with_model_fallbacks,
)

OVERLOADED = ("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is "
              "currently experiencing high demand. Spikes in demand are usually "
              "temporary. Please try again later.', 'status': 'UNAVAILABLE'}}")
OUT_OF_QUOTA = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
                "generate_content_free_tier_requests, limit: PerDay")


class FakeModel(Runnable):
    """A chat model that answers, or fails with a given error."""

    def __init__(self, name, error=None):
        self.model = name
        self.error = error
        self.calls = 0
        self.bound = None

    def invoke(self, input, config=None, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return AIMessage(content=f"answer from {self.model}")

    def bind_tools(self, tools, **kwargs):
        self.bound = tools
        return self

    def with_structured_output(self, schema, **kwargs):
        self.bound = schema
        return self


class TestTransientClassification:
    @pytest.mark.parametrize("text, transient", [
        (OVERLOADED, True),
        ("502 Bad Gateway", True),
        ("504 Deadline Exceeded", True),
        ("The model is overloaded. Please try again.", True),
        (OUT_OF_QUOTA, False),           # the day's budget, not a blip
        ("400 API key not valid", False),
        ("404 NOT_FOUND: model does not exist", False),
        ("unconverted data remains: ,", False),
    ])
    def test_only_provider_blips_are_worth_another_model(self, text, transient):
        assert is_transient(Exception(text)) is transient


class TestFallbackOrder:
    def test_the_primary_answers_and_no_one_else_is_called(self):
        a, b = FakeModel("primary"), FakeModel("understudy")
        out = FallbackChatModel([a, b]).invoke("hi")
        assert "primary" in out.content
        assert (a.calls, b.calls) == (1, 0)

    def test_an_overloaded_primary_hands_over(self):
        a = FakeModel("flash-lite", RuntimeError(OVERLOADED))
        b = FakeModel("2.5-flash")
        out = FallbackChatModel([a, b]).invoke("hi")
        assert "2.5-flash" in out.content
        assert (a.calls, b.calls) == (1, 1)

    def test_exhausted_quota_stops_rather_than_spending_another_model(self):
        a = FakeModel("flash-lite", RuntimeError(OUT_OF_QUOTA))
        b = FakeModel("2.5-flash")
        with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
            FallbackChatModel([a, b]).invoke("hi")
        assert b.calls == 0

    def test_a_programming_error_is_not_masked(self):
        a = FakeModel("flash-lite", ValueError("bad prompt"))
        b = FakeModel("2.5-flash")
        with pytest.raises(ValueError):
            FallbackChatModel([a, b]).invoke("hi")
        assert b.calls == 0

    def test_the_last_error_surfaces_when_every_model_is_down(self):
        models = [FakeModel("one", RuntimeError(OVERLOADED)),
                  FakeModel("two", RuntimeError("504 Deadline Exceeded"))]
        with pytest.raises(RuntimeError, match="504"):
            FallbackChatModel(models).invoke("hi")


class TestChatModelSurface:
    def test_bound_tools_reach_every_model(self):
        a = FakeModel("primary", RuntimeError(OVERLOADED))
        b = FakeModel("understudy")
        bound = FallbackChatModel([a, b]).bind_tools(["a_tool"])
        bound.invoke("hi")
        assert a.bound == ["a_tool"] and b.bound == ["a_tool"]

    def test_structured_output_reaches_every_model(self):
        a = FakeModel("primary", RuntimeError(OVERLOADED))
        b = FakeModel("understudy")
        FallbackChatModel([a, b]).with_structured_output(dict).invoke("hi")
        assert a.bound is dict and b.bound is dict

    def test_it_composes_behind_a_prompt_like_a_chat_model(self):
        chain = ChatPromptTemplate.from_messages([("system", "s"), ("human", "{q}")]) | (
            FallbackChatModel([FakeModel("primary", RuntimeError(OVERLOADED)),
                               FakeModel("understudy")]))
        assert "understudy" in chain.invoke({"q": "hi"}).content

    def test_unknown_attributes_come_from_the_primary(self):
        a = FakeModel("primary")
        a.temperature = 0.3
        assert FallbackChatModel([a, FakeModel("b")]).temperature == 0.3


class TestWiring:
    def test_no_fallbacks_configured_returns_the_model_untouched(self):
        primary = FakeModel("primary")
        assert with_model_fallbacks(primary, lambda n: FakeModel(n), []) is primary

    def test_an_unbuildable_understudy_is_skipped_not_fatal(self):
        def build(name):
            raise RuntimeError(f"no key for {name}")

        primary = FakeModel("primary")
        assert with_model_fallbacks(primary, build, ["missing"], "primary") is primary

    def test_the_primary_is_never_listed_twice(self):
        wrapped = with_model_fallbacks(FakeModel("same"), lambda n: FakeModel(n),
                                       ["same"], "same")
        assert isinstance(wrapped, FakeModel)  # nothing to fall back to

    def test_fallbacks_are_built_in_order(self):
        wrapped = with_model_fallbacks(FakeModel("primary", RuntimeError(OVERLOADED)),
                                       lambda n: FakeModel(n), ["second", "third"],
                                       "primary")
        assert wrapped.names == ["primary", "second", "third"]
        assert "second" in wrapped.invoke("hi").content
