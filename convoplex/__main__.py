"""CLI: generate, demo, verify, serve."""

from __future__ import annotations

import argparse
import sys

from convoplex.config import Settings
from convoplex.demo import format_demo, run_demo
from convoplex.integrations.images import clear_promo_cards
from convoplex.llm import LLMError
from convoplex.synthetic.generate import DEFAULT_CUSTOMERS, DEFAULT_SEED, generate
from convoplex.tunnel import callback_url, format_webhook_banner, start_quick_tunnel
from convoplex.whatsapp_flow import run_check, run_customer, run_send_template, store_for


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
    demo_cmd.add_argument(
        "--voice",
        default=None,
        metavar="PATH",
        help="OGG or WAV voice-note reply. Transcribed, then handled like --reply.",
    )
    demo_cmd.add_argument(
        "--promo-card",
        action="store_true",
        help="Draw a promo card for the offer and print its path.",
    )

    verify_cmd = sub.add_parser("verify", help="Check the hash-chained action log")
    verify_cmd.add_argument("path", nargs="?", default=None)

    whatsapp_cmd = sub.add_parser("whatsapp", help="WhatsApp Cloud API test-number commands")
    wa_sub = whatsapp_cmd.add_subparsers(dest="wa_cmd", required=True)
    wa_sub.add_parser("check", help="GET the phone number and print the display number and quality")
    wa_sub.add_parser("send-template", help="Send the hello_world template to WHATSAPP_DEMO_TO")
    customer_cmd = wa_sub.add_parser("customer", help="Map inbound WhatsApp messages to a synthetic customer")
    customer_cmd.add_argument("customer_id", type=int)

    serve_cmd = sub.add_parser("serve", help="Run the FastAPI app")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument(
        "--channel",
        choices=("simulator", "whatsapp"),
        default=None,
        help="simulator (default) or whatsapp. Live mode requires WHATSAPP_APP_SECRET and WHATSAPP_DEMO_TO.",
    )
    serve_cmd.add_argument(
        "--tunnel",
        action="store_true",
        help="Start a cloudflared quick tunnel and print the Meta callback URL.",
    )

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "generate":
        path = generate(settings.db_path, n_customers=args.customers, seed=args.seed)
        if settings.log_path.exists():
            settings.log_path.unlink()
        clear_promo_cards(settings.output_dir)
        store_for(settings).reset()
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
                voice_path=args.voice,
                promo_card=args.promo_card,
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

    if args.cmd == "whatsapp":
        if args.wa_cmd == "check":
            return run_check(settings)
        if args.wa_cmd == "send-template":
            return run_send_template(settings)
        if args.wa_cmd == "customer":
            return run_customer(settings, args.customer_id)
        parser.error(f"unknown whatsapp command {args.wa_cmd}")
        return 2

    if args.cmd == "serve":
        import uvicorn

        from convoplex.api import create_app

        if args.channel:
            settings = settings.with_channel(args.channel)
        refusal = _live_app_secret_error(settings)
        if refusal:
            print(refusal, file=sys.stderr)
            return 1
        app = create_app(settings)
        _announce_webhook(args, settings)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    parser.error(f"unknown command {args.cmd}")
    return 2


def _live_app_secret_error(settings: Settings) -> str | None:
    """Live WhatsApp must not serve a public webhook that accepts unsigned posts."""
    if settings.channel == "whatsapp" and not settings.whatsapp_app_secret:
        return (
            "Refusing to start: WhatsApp live requires WHATSAPP_APP_SECRET. "
            "Without it, anyone who can reach the webhook could post fake inbound messages. "
            "Set WHATSAPP_APP_SECRET from the Meta app, or serve with the simulator channel."
        )
    return None


def _announce_webhook(args, settings: Settings) -> None:
    """Print the callback URL and verify token when WhatsApp or a tunnel is requested."""
    if settings.channel != "whatsapp" and not args.tunnel:
        return
    tunnel = None
    public = None
    if args.tunnel:
        tunnel, public = start_quick_tunnel(args.port)
    host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    base = public or f"http://{host}:{args.port}"
    print(
        format_webhook_banner(
            callback=callback_url(base),
            verify_token=settings.whatsapp_verify_token,
            public=bool(public),
        )
    )
    if args.tunnel and public is None:
        if tunnel is None:
            print("cloudflared is not on PATH. Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        else:
            print("cloudflared did not report a public URL yet. Paste the https origin from its log.")
    if tunnel is not None:
        # The tunnel is a child process. uvicorn blocks; stop cloudflared when it returns.
        import atexit

        atexit.register(_stop_tunnel, tunnel)


def _stop_tunnel(proc) -> None:
    if proc.poll() is None:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
