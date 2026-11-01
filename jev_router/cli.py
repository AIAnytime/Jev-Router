import argparse
import asyncio
import os
import sys

from . import __version__
from .config import STRATEGIES, get_settings


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("jev_router.main:app", host=args.host, port=args.port, reload=args.reload, log_level=args.log_level)


def _eval(args: argparse.Namespace) -> None:
    from .evals import run_eval

    settings = get_settings()
    if not settings.jev_api_key:
        print("JEV_API_KEY is not set; only the heuristic baseline will be evaluated.", file=sys.stderr)
    asyncio.run(run_eval(args.dataset, args.strategy, settings, compare=not args.jev_only, as_json=args.json, concurrency=args.concurrency))


def _models(_: argparse.Namespace) -> None:
    from .registry import Registry

    registry = Registry.load(get_settings().jev_router_config_dir)
    print(f"{'MODEL':<34}{'STATUS':<11}{'QUALITY':>8}{'SPEED':>7}{'$IN/1M':>9}{'$OUT/1M':>9}  CONTEXT")
    for m in registry.models:
        status = "available" if registry.is_available(m) else "no key" if registry.provider_for(m).requires_key else "disabled"
        print(f"{m.id:<34}{status:<11}{m.quality:>8}{m.speed:>7}{m.input_cost:>9}{m.output_cost:>9}  {m.context_window:,}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jev-router", description="Decision-first AI router powered by Jev.")
    parser.add_argument("--version", action="version", version=f"jev-router {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the router API and dashboard")
    serve.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_serve)

    ev = sub.add_parser("eval", help="Evaluate routing decisions on a labelled dataset (no LLM calls)")
    ev.add_argument("--dataset", default="evals/routing_eval.jsonl")
    ev.add_argument("--strategy", default="balanced", choices=STRATEGIES)
    ev.add_argument("--jev-only", action="store_true", help="Skip the heuristic baseline")
    ev.add_argument("--concurrency", type=int, default=4)
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(func=_eval)

    models = sub.add_parser("models", help="List registry models and whether they are available")
    models.set_defaults(func=_models)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
