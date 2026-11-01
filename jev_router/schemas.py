from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RouterOptions(BaseModel):
    """Per-request routing controls, sent as `jev_router` in the body (OpenAI SDK: extra_body)."""

    strategy: str | None = None
    allowed_models: list[str] | None = None
    denied_models: list[str] | None = None
    max_cost: float | None = Field(None, description="Max estimated USD per request")
    min_quality: float | None = None
    privacy: Literal["cloud_allowed", "local_only"] = "cloud_allowed"
    fallbacks: bool = True
    guardrails: bool = False
    tool_top_k: int | None = Field(None, ge=1, description="Send only the k most relevant tools upstream")


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "auto"
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    jev_router: RouterOptions | None = None


class RouteModelRequest(BaseModel):
    messages: list[dict[str, Any]] | None = None
    prompt: str | None = None
    model: str = "auto"
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None
    jev_router: RouterOptions = Field(default_factory=RouterOptions)

    def to_messages(self) -> list[dict[str, Any]]:
        if self.messages:
            return self.messages
        if self.prompt:
            return [{"role": "user", "content": self.prompt}]
        raise ValueError("provide `messages` or `prompt`")


class AgentInput(BaseModel):
    name: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class RouteAgentRequest(BaseModel):
    task: str = Field(min_length=1)
    context: dict[str, Any] | None = None
    agents: list[AgentInput] | None = None


class RouteToolRequest(BaseModel):
    task: str | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] = Field(min_length=1)
    top_k: int = Field(3, ge=1)


class RoutePolicyRequest(BaseModel):
    content: str = Field(min_length=1)
    action: str | None = Field(None, description="The action an agent intends to take, if any")
    context: dict[str, Any] | None = None


class FeedbackRequest(BaseModel):
    score: int = Field(ge=-1, le=1, description="-1 bad routing, 0 neutral, 1 good routing")
    comment: str | None = None
