import time
from typing import Any

from ..engine.decision import COMPLEXITY_CRITERIA, label_from_score
from ..engine.features import extract_features, tool_name, truncate
from ..engine.heuristic import heuristic_signals, overlap_scores
from ..engine.jev_client import JevClient, JevError
from ..registry import AgentSpec
from ..schemas import RouteAgentRequest, RouteToolRequest

APPROVAL_THRESHOLD = 0.6


def _ranked(probs: dict[str, float], k: int | None = None) -> list[dict[str, Any]]:
    items = sorted(probs.items(), key=lambda kv: -kv[1])
    return [{"name": n, "probability": round(p, 4)} for n, p in items[:k]]


class AgentRouter:
    """Chooses which agent/workflow should execute a task."""

    def __init__(self, jev: JevClient, agents: list[AgentSpec]):
        self.jev = jev
        self.agents = agents

    async def route(self, req: RouteAgentRequest) -> dict[str, Any]:
        agents = [AgentSpec(**a.model_dump()) for a in req.agents] if req.agents else self.agents
        if not agents:
            raise ValueError("no agents configured; add config/agents.yaml or pass `agents`")
        by_name = {a.name: a for a in agents}
        criteria = {a.name: a.jev_description() for a in agents}
        start = time.perf_counter()
        source, error = "jev", None
        approval_p = multi_p = None
        cx_label, cx_score, confidence = None, None, None

        try:
            state: dict[str, Any] = {"task": truncate(req.task, 8000), "available_agents": criteria}
            if req.context:
                state["context"] = req.context
            questions: dict[str, Any] = {
                "agent_route": {
                    "type": "choice",
                    "instructions": "Which agent is best suited to execute this task end to end?",
                    "criteria": criteria,
                },
                "complexity": {"type": "score", "instructions": "How complex is this task to execute?", "criteria": COMPLEXITY_CRITERIA},
                "needs_approval": {
                    "type": "noul",
                    "instructions": "Would executing this task involve a sensitive or irreversible action (payments, refunds, deleting data, contacting customers, changing permissions) that needs human approval?",
                },
                "multi_agent": {
                    "type": "noul",
                    "instructions": "Does this task need several different specialist agents working together?",
                },
            }
            if len(agents) == 1:
                questions.pop("agent_route")
            data = await self.jev.ask(state, questions)
            ans = data["answers"]
            route = ans.get("agent_route") or {}
            probs = {k: float(v) for k, v in (route.get("probabilities") or {agents[0].name: 1.0}).items() if k in by_name}
            choice = route.get("choice") if route.get("choice") in by_name else max(probs, key=probs.get)
            confidence = float(route.get("confidence", probs.get(choice, 1.0)))
            cx = ans.get("complexity") or {}
            cx_label, cx_score = (label_from_score(cx), cx.get("score")) if cx else (None, None)
            approval_p = (ans.get("needs_approval") or {}).get("noul")
            multi_p = (ans.get("multi_agent") or {}).get("noul")
        except JevError as e:
            source, error = "heuristic_fallback", str(e)
            probs = overlap_scores(req.task, criteria)
            choice = max(probs, key=probs.get)
            confidence = probs[choice]
            sig = heuristic_signals(extract_features([{"role": "user", "content": req.task}]), [])
            cx_label, cx_score = sig.complexity_label, sig.complexity

        agent = by_name[choice]
        requires_approval = agent.requires_approval or (approval_p is not None and approval_p >= APPROVAL_THRESHOLD)
        return {
            "agent": agent.name,
            "confidence": round(confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in probs.items()},
            "ranking": _ranked(probs),
            "complexity": cx_label,
            "complexity_score": cx_score,
            "requires_approval": requires_approval,
            "approval_probability": approval_p,
            "multi_agent_probability": multi_p,
            "tools": agent.tools,
            "decision_engine": source,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "error": error,
        }


class ToolRouter:
    """Decides whether a tool is needed and which of the provided tools to call."""

    def __init__(self, jev: JevClient):
        self.jev = jev

    async def route(self, req: RouteToolRequest) -> dict[str, Any]:
        if not (req.task or req.messages):
            raise ValueError("provide `task` or `messages`")
        task = req.task or extract_features(req.messages or []).prompt
        criteria: dict[str, str] = {}
        for t in req.tools:
            fn = t.get("function") or t
            criteria[tool_name(t)] = fn.get("description") or tool_name(t)
        start = time.perf_counter()
        source, error = "jev", None
        try:
            questions: dict[str, Any] = {
                "needs_tool": {
                    "type": "noul",
                    "instructions": "Does fulfilling this request require calling one of the available tools, instead of answering directly from knowledge?",
                },
                "tool_route": {
                    "type": "choice",
                    "instructions": "Which tool should be called first to fulfil this request?",
                    "criteria": criteria,
                },
            }
            if len(criteria) == 1:
                questions.pop("tool_route")
            data = await self.jev.ask({"request": truncate(task, 8000), "available_tools": criteria}, questions)
            ans = data["answers"]
            needs_p = float((ans.get("needs_tool") or {}).get("noul", 1.0))
            route = ans.get("tool_route") or {}
            probs = {k: float(v) for k, v in (route.get("probabilities") or {next(iter(criteria)): 1.0}).items() if k in criteria}
            choice = route.get("choice") if route.get("choice") in criteria else max(probs, key=probs.get)
            confidence = float(route.get("confidence", probs.get(choice, 1.0)))
        except JevError as e:
            source, error = "heuristic_fallback", str(e)
            probs = overlap_scores(task, criteria)
            choice = max(probs, key=probs.get)
            confidence = probs[choice]
            needs_p = 0.5

        needs_tool = needs_p >= 0.5
        return {
            "needs_tool": needs_tool,
            "needs_tool_probability": round(needs_p, 4),
            "tool": choice if needs_tool else None,
            "confidence": round(confidence, 4),
            "ranking": _ranked(probs, req.top_k),
            "tool_choice": {"type": "function", "function": {"name": choice}} if needs_tool else "none",
            "decision_engine": source,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "error": error,
        }
