from ..registry import ModelSpec
from ..schemas import RouterOptions
from .features import RequestFeatures


def _matches(model: ModelSpec, names: list[str]) -> bool:
    return model.id in names or model.model in names


def filter_models(
    models: list[ModelSpec], features: RequestFeatures, opts: RouterOptions
) -> tuple[list[ModelSpec], dict[str, str]]:
    """Apply hard constraints so Jev only ever chooses between models that can serve the request."""
    eligible: list[ModelSpec] = []
    rejected: dict[str, str] = {}
    needs = features.needs

    for m in models:
        reason = None
        if opts.allowed_models and not _matches(m, opts.allowed_models):
            reason = "not in allowed_models"
        elif opts.denied_models and _matches(m, opts.denied_models):
            reason = "in denied_models"
        elif m.only_when and not needs.intersection(m.only_when):
            reason = f"specialist model, only used for {'/'.join(m.only_when)} input"
        elif "image" in needs and "image" not in m.modalities:
            reason = "no image input support"
        elif "audio" in needs and "audio" not in m.modalities:
            reason = "no audio input support"
        elif "tools" in needs and not m.supports_tools:
            reason = "no tool calling support"
        elif "json" in needs and not m.supports_json:
            reason = "no JSON mode support"
        elif features.input_tokens + features.output_tokens > m.context_window:
            reason = (
                f"context window {m.context_window} < ~{features.input_tokens + features.output_tokens} tokens needed"
            )
        elif opts.privacy == "local_only" and not m.local:
            reason = "privacy=local_only and model is cloud-hosted"
        elif opts.min_quality is not None and m.quality < opts.min_quality:
            reason = f"quality {m.quality} < min_quality {opts.min_quality}"
        elif opts.max_cost is not None and (
            est := m.estimate_cost(features.input_tokens, features.output_tokens)
        ) > opts.max_cost:
            reason = f"estimated cost ${est:.6f} > max_cost ${opts.max_cost}"

        if reason:
            rejected[m.id] = reason
        else:
            eligible.append(m)
    return eligible, rejected
