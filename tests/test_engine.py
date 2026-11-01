import time

import pytest

from jev_router.engine.cache import TTLCache
from jev_router.engine.decision import label_from_score, parse_answers, routing_questions
from jev_router.engine.features import extract_features, truncate
from jev_router.engine.filters import filter_models
from jev_router.engine.heuristic import Signals, heuristic_signals, required_level
from jev_router.engine.policy import PolicyEngine
from jev_router.providers.anthropic import from_anthropic, to_anthropic
from jev_router.registry import GuardrailCheck, GuardrailConfig, ModelSpec, Registry
from jev_router.schemas import RouterOptions
from jev_router.services.guardrails import Guardrails

from .conftest import ROOT


def model(id_, quality, speed=7.0, inp=0.1, out=0.5, **kw) -> ModelSpec:
    return ModelSpec(id=id_, provider="test", model=id_, description=id_, quality=quality, speed=speed,
                     input_cost=inp, output_cost=out, **kw)


SMALL = model("small", 5.0, speed=9, inp=0.05, out=0.2)
MID = model("mid", 7.0, speed=7, inp=0.5, out=2.0)
BIG = model("big", 9.5, speed=4, inp=3.0, out=15.0)
POOL = [SMALL, MID, BIG]


def signals(probs, complexity, premium=0.0, source="jev", confidence=0.9) -> Signals:
    return Signals(source=source, model_probs=probs, choice=max(probs, key=probs.get), choice_confidence=confidence,
                   complexity=complexity, complexity_label="x", complexity_confidence=0.9, premium_prob=premium,
                   task_type="coding", task_confidence=0.9)


def user(text):
    return [{"role": "user", "content": text}]


# -- features -------------------------------------------------------------

def test_features_pick_last_user_turn_and_detect_needs():
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                     {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
    ]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    f = extract_features(messages, {"tools": tools, "response_format": {"type": "json_object"}, "max_tokens": 50})
    assert f.prompt == "what is this?"
    assert f.system == "Be terse."
    assert f.turns == 2
    assert f.needs == {"image", "tools", "json"}
    assert f.tool_names == ["lookup"]
    assert f.output_tokens == 50
    assert f.input_tokens > 800


def test_truncate_keeps_head_and_tail():
    text = "a" * 700 + "b" * 300 + "c" * 1000
    out = truncate(text, 1000)
    assert out.startswith("a" * 700) and out.endswith("c" * 300) and "[truncated]" in out


# -- filters --------------------------------------------------------------

def test_filters_apply_hard_constraints():
    vl = model("vl", 4.5, modalities=["text", "image"], only_when=["image"])
    local = model("local", 3.0, local=True, inp=0, out=0)
    tiny_ctx = model("tiny", 6.0, context_window=100)
    no_tools = model("notools", 6.0, supports_tools=False)
    pool = [SMALL, BIG, vl, local, tiny_ctx, no_tools]

    eligible, rejected = filter_models(pool, extract_features(user("hi")), RouterOptions())
    assert "vl" in rejected and "specialist" in rejected["vl"]
    assert "tiny" in rejected and "context window" in rejected["tiny"]

    img = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]
    eligible, _ = filter_models(pool, extract_features(img), RouterOptions())
    assert [m.id for m in eligible] == ["vl"]

    eligible, _ = filter_models(pool, extract_features(user("hi")), RouterOptions(privacy="local_only"))
    assert [m.id for m in eligible] == ["local"]

    tools = {"tools": [{"type": "function", "function": {"name": "t"}}]}
    _, rejected = filter_models(pool, extract_features(user("hi"), tools), RouterOptions())
    assert rejected["notools"] == "no tool calling support"

    eligible, rejected = filter_models(pool, extract_features(user("hi")), RouterOptions(max_cost=0.001, denied_models=["small"]))
    assert "big" in rejected and "max_cost" in rejected["big"]
    assert rejected["small"] == "in denied_models"

    eligible, _ = filter_models(pool, extract_features(user("hi")), RouterOptions(allowed_models=["big"]))
    assert [m.id for m in eligible] == ["big"]


# -- policy ---------------------------------------------------------------

@pytest.fixture(scope="module")
def policy() -> PolicyEngine:
    return PolicyEngine(Registry.load(ROOT / "config").router, 0.55)


def test_simple_requests_go_to_small_models(policy):
    f = extract_features(user("hey there"))
    plan = policy.rank(POOL, signals({"small": 0.8, "mid": 0.15, "big": 0.05}, 0.1), f, "balanced")
    assert plan.selected.model.id == "small"
    assert plan.baseline.id == "big"
    assert "balanced strategy chose small" in plan.reason


def test_hard_requests_go_to_capable_models(policy):
    f = extract_features(user("design a distributed database"))
    plan = policy.rank(POOL, signals({"small": 0.05, "mid": 0.15, "big": 0.8}, 2.9, premium=0.95), f, "balanced")
    assert plan.selected.model.id == "big"
    assert plan.required_level > 0.9
    # The small model cannot meet the required capability and is dropped from the chain entirely.
    assert "small" not in [r.model.id for r in plan.ranked]


def test_strategies_change_the_choice(policy):
    f = extract_features(user("write a sorting function"))
    s = signals({"small": 0.3, "mid": 0.4, "big": 0.3}, 1.2, premium=0.2)
    assert policy.rank(POOL, s, f, "cheapest").selected.model.id == "small"
    assert policy.rank(POOL, s, f, "quality").selected.model.id == "big"
    assert policy.rank(POOL, s, f, "fastest").selected.model.id == "small"
    local = model("local", 5.5, local=True, inp=0, out=0)
    assert policy.rank(POOL + [local], s, f, "privacy").selected.model.id == "local"


def test_low_confidence_biases_toward_stronger_models(policy):
    f = extract_features(user("hmm"))
    s = signals({"small": 0.4, "mid": 0.35, "big": 0.25}, 1.5, premium=0.3, confidence=0.4)
    plan = policy.rank(POOL, s, f, "balanced")
    assert plan.low_confidence
    assert plan.required_level == pytest.approx(required_level(1.5, 0.3) + 0.1, abs=1e-3)
    assert "below threshold" in plan.reason


# -- decision parsing ------------------------------------------------------

def test_routing_questions_skip_model_choice_for_single_candidate():
    assert "model_route" not in routing_questions([SMALL])
    assert set(routing_questions(POOL)) == {"model_route", "complexity", "premium_required", "task_type"}


def test_parse_answers_maps_jev_types():
    data = {
        "model": "jev-1",
        "answers": {
            "model_route": {"choice": "mid", "confidence": 0.7, "probabilities": {"small": 0.1, "mid": 0.7, "big": 0.2, "ghost": 0.5}},
            "complexity": {"score": 2.2, "confidence": 0.8, "probabilities": {"0": 0.0, "1": 0.1, "2": 0.7, "3": 0.2}},
            "premium_required": {"noul": 0.4},
            "task_type": {"choice": "coding", "confidence": 0.95},
            "guard_pii": {"noul": 0.1},
        },
    }
    fallback = heuristic_signals(extract_features(user("x")), POOL)
    s = parse_answers(data, POOL, fallback)
    assert s.choice == "mid" and s.choice_confidence == 0.7
    assert "ghost" not in s.model_probs
    assert s.complexity == 2.2 and s.complexity_label == "complex"
    assert s.premium_prob == 0.4
    assert s.task_type == "coding"
    assert s.extra == {"guard_pii": {"noul": 0.1}}
    assert label_from_score({"score": 2.9}) == "expert"


def test_heuristic_flags_hard_coding_prompts():
    f = extract_features(user("Build a production-ready distributed FastAPI backend with authentication"))
    s = heuristic_signals(f, POOL)
    assert s.task_type == "coding"
    assert s.complexity >= 2
    simple = heuristic_signals(extract_features(user("hello!")), POOL)
    assert simple.task_type == "chat" and simple.complexity_label == "simple"


# -- cache / guardrails ----------------------------------------------------

def test_ttl_cache_expires_and_evicts():
    c = TTLCache(maxsize=2, ttl=0.05)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") is None and c.get("c") == 3
    time.sleep(0.06)
    assert c.get("c") is None
    assert c.stats()["hits"] == 1


def test_guardrail_verdicts():
    cfg = GuardrailConfig(checks={
        "harmful": GuardrailCheck(instructions="harm?", review=0.6, deny=0.85),
        "pii": GuardrailCheck(instructions="pii?", review=0.6),
    })
    g = Guardrails(cfg)
    assert set(g.questions()) == {"guard_harmful", "guard_pii"}
    assert g.evaluate({"guard_harmful": {"noul": 0.1}, "guard_pii": {"noul": 0.2}}).verdict == "allow"
    review = g.evaluate({"guard_harmful": {"noul": 0.1}, "guard_pii": {"noul": 0.7}})
    assert review.verdict == "review" and not g.blocks(review)
    deny = g.evaluate({"guard_harmful": {"noul": 0.9}, "guard_pii": {"noul": 0.7}})
    assert deny.verdict == "deny" and g.blocks(deny)


# -- anthropic adapter -----------------------------------------------------

def test_anthropic_request_translation():
    claude = model("claude", 9.0)
    payload = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "18C"},
        ],
        "tools": [{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object"}}}],
        "tool_choice": "required",
        "stop": "END",
        "max_tokens": 77,
    }
    body = to_anthropic(claude, payload)
    assert body["system"] == "sys"
    assert body["max_tokens"] == 77
    assert body["stop_sequences"] == ["END"]
    assert body["tool_choice"] == {"type": "any"}
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][-1] == {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_response_translation():
    out = from_anthropic({
        "id": "msg_1", "content": [{"type": "text", "text": "Sure."},
                                   {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}],
        "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 3},
    }, "claude")
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Sure."
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
