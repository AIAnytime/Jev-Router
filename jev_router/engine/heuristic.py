import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..registry import ModelSpec
from .features import RequestFeatures

COMPLEXITY_LABELS = ["simple", "moderate", "complex", "expert"]
COMPLEXITY_CRITERIA = [
    "Simple: greetings, small talk, single facts, short rewrites or one-line answers",
    "Moderate: explanations, standard how-to questions, short code snippets, routine writing",
    "Complex: multi-part problems, real applications, non-trivial code, careful analysis",
    "Expert: research-grade reasoning, large production systems, rigorous proofs, deep domain expertise",
]

TASK_TYPES = {
    "coding": "Writing, debugging, reviewing or explaining code, software architecture, APIs or DevOps",
    "math": "Mathematics, quantitative problems, proofs or calculations",
    "reasoning": "Logic puzzles, planning, multi-step problem solving or decision making",
    "analysis": "Analyzing documents, data or arguments, comparing options, evaluating trade-offs",
    "writing": "Creative or professional writing such as stories, emails, marketing copy or essays",
    "summarization": "Summarizing or condensing provided text",
    "extraction": "Extracting structured data, classifying, tagging or reformatting into JSON",
    "translation": "Translating text between languages",
    "chat": "Casual conversation, greetings or simple factual questions",
    "vision": "Understanding images, screenshots, charts or documents",
    "audio": "Understanding speech or audio",
}

_KEYWORDS = {
    "coding": ["code", "python", "javascript", "typescript", "function", "bug", "debug", "api", "fastapi",
               "sql", "regex", "refactor", "compile", "deploy", "docker", "kubernetes", "react", "rust",
               "golang", "java", "script", "unit test", "endpoint", "database", "backend", "frontend"],
    "math": ["prove", "proof", "integral", "derivative", "equation", "theorem", "probability", "calculate",
             "solve for", "matrix", "eigen"],
    "reasoning": ["step by step", "plan ", "strategy", "trade-off", "tradeoff", "logic", "puzzle", "riddle"],
    "analysis": ["analyze", "analyse", "compare", "evaluate", "assess", "pros and cons", "critique"],
    "writing": ["story", "poem", "essay", "blog", "email", "tweet", "marketing", "cover letter", "draft",
                "slogan", "lyrics"],
    "summarization": ["summarize", "summarise", "tl;dr", "tldr", "key points", "condense"],
    "extraction": ["extract", "classify", "json", "parse", "categorize", "categorise", "label"],
    "translation": ["translate", "in spanish", "in french", "in german", "into english", "in hindi"],
}
_HARD = ["production", "scalable", "architecture", "distributed", "optimiz", "optimis", "prove",
         "authentication", "concurren", "security", "end-to-end", "full-stack", "microservice",
         "benchmark", "rigorous", "advanced", "multi-step", "from scratch", "complete", "detailed"]
_CODE = re.compile(r"```|\bdef \w+\(|\bclass \w+|function\s*\w*\(|=>|#include|\bSELECT\b.+\bFROM\b", re.I | re.S)


def required_level(complexity: float, premium_prob: float) -> float:
    """Quality level (0..1) the request needs, from Jev's complexity score (0..3) and premium probability."""
    return max(0.0, min(1.0, 0.7 * (complexity / 3) + 0.3 * premium_prob))


@dataclass
class Signals:
    source: str
    model_probs: dict[str, float]
    choice: str | None
    choice_confidence: float
    complexity: float
    complexity_label: str
    complexity_confidence: float
    premium_prob: float
    task_type: str
    task_confidence: float
    extra: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    jev_model: str | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


def _softmax_fit(eligible: list[ModelSpec], level: float, task: str) -> dict[str, float]:
    weights = {}
    for m in eligible:
        w = math.exp(-abs(m.quality / 10 - level) * 6)
        if any(task in s.lower() for s in m.strengths):
            w *= 1.3
        weights[m.id] = w
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 4) for k, v in weights.items()}


def heuristic_signals(
    features: RequestFeatures, eligible: list[ModelSpec], source: str = "heuristic", error: str | None = None
) -> Signals:
    text = f"{features.system}\n{features.prompt}".lower()
    if "image" in features.needs:
        task = "vision"
    elif "audio" in features.needs:
        task = "audio"
    else:
        hits = {t: sum(text.count(k) for k in kws) for t, kws in _KEYWORDS.items()}
        best = max(hits, key=hits.get)
        task = best if hits[best] else "chat"
        if _CODE.search(features.prompt):
            task = "coding"

    tokens = len(features.prompt) // 4
    complexity = 0.0
    if tokens > 40 or task in ("coding", "math", "reasoning", "analysis"):
        complexity += 1.0
    complexity += min(1.5, 0.5 * sum(1 for k in _HARD if k in text))
    if tokens > 1500:
        complexity += 0.5
    if _CODE.search(features.prompt):
        complexity += 0.5
    if features.turns > 6:
        complexity += 0.25
    complexity = min(3.0, complexity)
    premium = max(0.0, min(1.0, (complexity - 1.2) / 1.8))

    probs = _softmax_fit(eligible, required_level(complexity, premium), task)
    choice = max(probs, key=probs.get) if probs else None
    return Signals(
        source=source,
        model_probs=probs,
        choice=choice,
        choice_confidence=probs.get(choice, 0.0) if choice else 0.0,
        complexity=round(complexity, 2),
        complexity_label=COMPLEXITY_LABELS[min(3, round(complexity))],
        complexity_confidence=0.5,
        premium_prob=round(premium, 3),
        task_type=task,
        task_confidence=0.5,
        error=error,
    )


_WORD = re.compile(r"[a-z0-9]+")


def overlap_scores(text: str, options: dict[str, str]) -> dict[str, float]:
    """Keyword-overlap distribution over named options; the offline fallback for agent/tool routing."""
    words = set(_WORD.findall(text.lower()))
    raw = {}
    for name, desc in options.items():
        vocab = {w for w in _WORD.findall(f"{name} {desc}".lower().replace("_", " ")) if len(w) > 2}
        raw[name] = 0.1 + sum(1 for w in vocab if w in words or w.rstrip("s") in words)
    total = sum(raw.values()) or 1.0
    return {k: round(v / total, 4) for k, v in raw.items()}
