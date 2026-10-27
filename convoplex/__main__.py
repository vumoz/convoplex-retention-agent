"""CLI: generate, demo, verify, serve."""

from __future__ import annotations

import argparse
import sys

from convoplex.config import Settings
from convoplex.demo import format_demo, run_demo
from convoplex.llm import LLMError
from convoplex.synthetic.generate import DEFAULT_CUSTOMERS, DEFAULT_SEED, generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="convoplex", description="Convoplex Retention Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    generate_cmd = sub.add_parser("generate", help="Write a fresh synthetic SQLite database")
    generate_cmd.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    generate_cmd.add_argument("--seed", type=int, default=DEFAULT_SEED)

    demo_cmd = sub.add_parser("demo", help="Run a win-back and one or more customer replies")
    demo_cmd.add_argument("--live", action="store_true", help="Call Muse Spark (requires META_API_KEY)")
    demo_cmd.add_argument("--mock", action="store_true", help="Force the deterministic mock")
    demo_cmd.add_argument("--reset", action="store_true", help="Regenerate the synthetic database first")
    demo_cmd.add_argument("--customer", type=int, default=None, help="Customer id. Default picks a lapsed Pidgin regular.")
    demo_cmd.add_argument("--lang", choices=("en", "pcm"), default=None, help="Override the spoken language for this run.")
    demo_cmd.add_argument(
        "--reply",
        action="append",
        default=None,
        help="Customer reply. Repeat for decline, question, then accept. Default is one accepting reply.",
    )

    verify_cmd = sub.add_parser("verify", help="Check the hash-chained action log")
    verify_cmd.add_argument("path", nargs="?", default=None)

    serve_cmd = sub.add_parser("serve", help="Run the FastAPI app")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "generate":
        path = generate(settings.db_path, n_customers=args.customers, seed=args.seed)
        if settings.log_path.exists():
            settings.log_path.unlink()
        print(f"Wrote synthetic database to {path}")
        return 0

    if args.cmd == "demo":
        if args.live and args.mock:
            parser.error("choose only one of --live and --mock")
        if args.live:
            settings = settings.with_llm_mode("live")
        elif args.mock:
            settings = settings.with_llm_mode("mock")
        try:
            result = run_demo(
                settings,
                reset_data=args.reset,
                customer_id=args.customer,
                language=args.lang,
                replies=args.reply,
            )
        except (LLMError, RuntimeError, LookupError, ValueError) as exc:
            print(f"Demo failed: {exc}", file=sys.stderr)
            return 1
        print(format_demo(result))
        return 0 if result.log_ok else 1

    if args.cmd == "verify":
        from convoplex.actionlog import ActionLog

        path = args.path or settings.log_path
        result = ActionLog(path).verify()
        if result.ok:
            print(f"OK {result.entries} entries")
            return 0
        print(f"FAIL {result.error}", file=sys.stderr)
        return 1

    if args.cmd == "serve":
        import uvicorn

        from convoplex.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
