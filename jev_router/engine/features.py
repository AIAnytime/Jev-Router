from dataclasses import dataclass, field
from typing import Any

IMAGE_TOKENS = 800
AUDIO_TOKENS = 1500
DEFAULT_OUTPUT_TOKENS = 1024


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _content_media(content: Any) -> tuple[int, int]:
    images = audio = 0
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            kind = p.get("type")
            if kind in ("image_url", "image"):
                images += 1
            elif kind in ("input_audio", "audio"):
                audio += 1
    return images, audio


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return text[:head] + "\n...[truncated]...\n" + text[-(limit - head) :]


@dataclass
class RequestFeatures:
    prompt: str
    system: str = ""
    history: str = ""
    turns: int = 1
    input_tokens: int = 0
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    needs: set[str] = field(default_factory=set)
    tool_names: list[str] = field(default_factory=list)

    def cache_key_parts(self) -> tuple:
        return (self.prompt, self.system[:2000], self.history[-2000:], tuple(sorted(self.needs)))


def tool_name(tool: dict[str, Any]) -> str:
    return (tool.get("function") or {}).get("name") or tool.get("name") or "tool"


def extract_features(messages: list[dict[str, Any]], payload: dict[str, Any] | None = None) -> RequestFeatures:
    payload = payload or {}
    system_parts: list[str] = []
    convo: list[tuple[str, str]] = []
    chars = images = audio = 0

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        text = content_text(content)
        img, aud = _content_media(content)
        images += img
        audio += aud
        chars += len(text)
        for call in msg.get("tool_calls") or []:
            chars += len(str((call.get("function") or {}).get("arguments", "")))
        if role in ("system", "developer"):
            system_parts.append(text)
        else:
            convo.append((role, text))

    user_turns = [t for r, t in convo if r == "user"]
    prompt = user_turns[-1] if user_turns else (convo[-1][1] if convo else "")
    history = "\n".join(f"{r}: {t}" for r, t in convo[:-1])

    tools = payload.get("tools") or []
    tool_chars = sum(len(str(t)) for t in tools)

    needs: set[str] = set()
    if images:
        needs.add("image")
    if audio:
        needs.add("audio")
    if tools:
        needs.add("tools")
    fmt = payload.get("response_format") or {}
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        needs.add("json")

    output = payload.get("max_completion_tokens") or payload.get("max_tokens") or DEFAULT_OUTPUT_TOKENS
    return RequestFeatures(
        prompt=prompt,
        system="\n".join(system_parts),
        history=history,
        turns=max(1, len(user_turns)),
        input_tokens=(chars + tool_chars) // 4 + images * IMAGE_TOKENS + audio * AUDIO_TOKENS + 4 * len(messages),
        output_tokens=int(output),
        needs=needs,
        tool_names=[tool_name(t) for t in tools],
    )
