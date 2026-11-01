import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()

traces = Table(
    "traces",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("created_at", Float, index=True, nullable=False),
    Column("endpoint", String(40)),
    Column("status", String(20)),
    Column("strategy", String(20)),
    Column("requested_model", String(160)),
    Column("selected_model", String(160), index=True),
    Column("provider", String(60)),
    Column("decision_source", String(30)),
    Column("task_type", String(40)),
    Column("complexity", String(20)),
    Column("confidence", Float),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("estimated_cost", Float),
    Column("actual_cost", Float),
    Column("baseline_cost", Float),
    Column("latency_ms", Float),
    Column("decision_ms", Float),
    Column("fallback_count", Integer),
    Column("stream", Boolean),
    Column("prompt_preview", Text),
    Column("error", Text),
    Column("feedback", Integer),
    Column("feedback_comment", Text),
    Column("data", Text),
)

SUMMARY_COLUMNS = [c for c in traces.columns if c.name != "data"]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


class TraceStore:
    def __init__(self, database_url: str):
        if database_url.startswith("sqlite"):
            db_path = database_url.split("///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(database_url)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def record(self, trace: dict[str, Any]) -> None:
        cost = trace.get("cost") or {}
        usage = trace.get("usage") or {}
        latency = trace.get("latency_ms") or {}
        row = {
            "id": trace["id"],
            "created_at": trace.get("created_at", time.time()),
            "endpoint": trace.get("endpoint"),
            "status": trace.get("status"),
            "strategy": trace.get("strategy"),
            "requested_model": trace.get("requested_model"),
            "selected_model": trace.get("selected_model"),
            "provider": trace.get("provider"),
            "decision_source": trace.get("decision_engine"),
            "task_type": trace.get("task_type"),
            "complexity": trace.get("complexity"),
            "confidence": trace.get("confidence"),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "estimated_cost": cost.get("estimated"),
            "actual_cost": cost.get("actual"),
            "baseline_cost": cost.get("baseline"),
            "latency_ms": latency.get("total"),
            "decision_ms": latency.get("decision"),
            "fallback_count": max(0, len(trace.get("attempts") or []) - 1),
            "stream": bool(trace.get("stream")),
            "prompt_preview": trace.get("prompt_preview"),
            "error": trace.get("error"),
            "data": json.dumps(trace, default=str),
        }
        async with self.engine.begin() as conn:
            await conn.execute(traces.insert().values(**row))

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        model: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        q = select(*SUMMARY_COLUMNS).order_by(traces.c.created_at.desc()).limit(limit).offset(offset)
        if model:
            q = q.where(traces.c.selected_model == model)
        if status:
            q = q.where(traces.c.status == status)
        async with self.engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(q)]

    async def count(self) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(traces))).scalar_one()

    async def get(self, trace_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(traces).where(traces.c.id == trace_id))).first()
        if not row:
            return None
        data = json.loads(row.data)
        data["feedback"] = row.feedback
        data["feedback_comment"] = row.feedback_comment
        return data

    async def set_feedback(self, trace_id: str, score: int, comment: str | None) -> bool:
        async with self.engine.begin() as conn:
            res = await conn.execute(
                update(traces)
                .where(traces.c.id == trace_id)
                .values(feedback=score, feedback_comment=comment)
            )
        return res.rowcount > 0

    async def analytics(self, hours: float = 24.0) -> dict[str, Any]:
        since = time.time() - hours * 3600
        q = (
            select(*SUMMARY_COLUMNS)
            .where(traces.c.created_at >= since)
            .order_by(traces.c.created_at.desc())
            .limit(50_000)
        )
        async with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in await conn.execute(q)]

        chat = [r for r in rows if r["endpoint"] == "chat"]
        ok = [r for r in chat if r["status"] == "success"]
        latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]
        decision = [r["decision_ms"] for r in chat if r["decision_ms"] is not None]
        actual = sum(r["actual_cost"] or 0 for r in ok)
        baseline = sum(r["baseline_cost"] or 0 for r in ok)

        by_model: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"requests": 0, "cost": 0.0, "latencies": [], "errors": 0}
        )
        for r in chat:
            m = by_model[r["selected_model"] or "none"]
            m["requests"] += 1
            if r["status"] == "success":
                m["cost"] += r["actual_cost"] or 0
                if r["latency_ms"] is not None:
                    m["latencies"].append(r["latency_ms"])
            else:
                m["errors"] += 1
        models = {
            name: {
                "requests": m["requests"],
                "share": round(m["requests"] / len(chat), 4) if chat else 0,
                "cost": round(m["cost"], 6),
                "avg_latency_ms": round(statistics.fmean(m["latencies"]), 1) if m["latencies"] else None,
                "errors": m["errors"],
            }
            for name, m in sorted(by_model.items(), key=lambda kv: -kv[1]["requests"])
        }

        step = 300 if hours <= 2 else 3600 if hours <= 48 else 86400
        now = time.time()
        first, last = int(since // step * step), int(now // step * step)
        buckets: dict[int, dict[str, float]] = {
            t: {"requests": 0, "cost": 0.0} for t in range(first, last + step, step)
        }
        for r in chat:
            b = int(r["created_at"] // step * step)
            if b not in buckets:
                continue
            buckets[b]["requests"] += 1
            buckets[b]["cost"] += r["actual_cost"] or 0
        rated = [r["feedback"] for r in rows if r["feedback"] is not None]

        return {
            "window_hours": hours,
            "requests": len(chat),
            "route_calls": len(rows) - len(chat),
            "success_rate": round(len(ok) / len(chat), 4) if chat else None,
            "fallback_rate": round(sum(1 for r in chat if (r["fallback_count"] or 0) > 0) / len(chat), 4)
            if chat
            else None,
            "cost": {
                "actual": round(actual, 6),
                "baseline": round(baseline, 6),
                "saved": round(baseline - actual, 6),
                "saved_pct": round((baseline - actual) / baseline * 100, 1) if baseline else None,
            },
            "latency_ms": {
                "avg": round(statistics.fmean(latencies), 1) if latencies else None,
                "p50": _pct(latencies, 50),
                "p95": _pct(latencies, 95),
                "decision_avg": round(statistics.fmean(decision), 1) if decision else None,
            },
            "tokens": {
                "input": sum(r["input_tokens"] or 0 for r in ok),
                "output": sum(r["output_tokens"] or 0 for r in ok),
            },
            "models": models,
            "strategies": dict(Counter(r["strategy"] for r in chat if r["strategy"])),
            "task_types": dict(Counter(r["task_type"] for r in chat if r["task_type"])),
            "complexity": dict(Counter(r["complexity"] for r in chat if r["complexity"])),
            "decision_sources": dict(Counter(r["decision_source"] for r in rows if r["decision_source"])),
            "feedback": {
                "count": len(rated),
                "good": sum(1 for f in rated if f > 0),
                "bad": sum(1 for f in rated if f < 0),
            },
            "bucket_seconds": step,
            "timeseries": [
                {"t": t, "requests": v["requests"], "cost": round(v["cost"], 6)}
                for t, v in sorted(buckets.items())
            ],
        }
