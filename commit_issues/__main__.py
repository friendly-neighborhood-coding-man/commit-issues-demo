"""Command-line entry point. External actions require an explicit operator command."""

import argparse
import os
import re
import sqlite3
import sys
from contextlib import closing

from .debate import MODELS
from .github import AppError, GATEWAY, GitHub
from .groq import DEFAULT_GROQ_MODEL, GROQ_MODELS
from .storage import Store
from .workflow import export_html, prepare, publish


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m commit_issues", description="Five emotions. One PR timeline.")
    parser.add_argument("--db", default=os.environ.get("COMMIT_ISSUES_DB", ".commit-issues/state.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Create a private draft")
    target = preview.add_mutually_exclusive_group(required=True)
    target.add_argument("--fixture", action="store_true", help="Use the supplied offline demo PR")
    target.add_argument("--pr", type=int, help="Read an opted-in PR via the Sprites Connector")
    preview.add_argument("--model", choices=(*MODELS, *GROQ_MODELS), help="Override AI_PROVIDER and GROQ_MODEL")
    preview.add_argument("--approve-paid-model", action="store_true", help="Authorize up to seven provider requests")
    preview.add_argument("--groq-smoke", action="store_true", help="Use one Groq request on the offline fixture")
    preview.add_argument("--html", help="Export a private HTML preview to a new file")
    show = commands.add_parser("show", help="Read a saved private draft")
    show.add_argument("draft")
    show.add_argument("--html")
    live = commands.add_parser("publish", help="Post one exact approved draft via the Sprites Connector")
    live.add_argument("--approve-comments", metavar="DRAFT_FINGERPRINT", required=True)
    return parser


def connector():
    return GitHub(os.environ.get("COMMIT_ISSUES_REPO"), os.environ.get("COMMIT_ISSUES_GITHUB_GATEWAY", GATEWAY))


def resolve_model(explicit_model):
    if explicit_model is not None:
        return explicit_model
    provider = os.environ.get("AI_PROVIDER", "mock")
    if provider == "mock":
        return "mock"
    if provider != "groq":
        raise AppError("Set AI_PROVIDER to mock or groq, or select a hosted model with --model.")
    model = os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    if model not in GROQ_MODELS:
        raise AppError(f"Set GROQ_MODEL to one of: {', '.join(GROQ_MODELS)}.")
    return model


def model_key(args):
    if args.model == "mock":
        return None
    if not args.approve_paid_model:
        raise AppError("A hosted model requires explicit --approve-paid-model for this private preview.")
    if args.model in GROQ_MODELS:
        variable = "GROQ_API_KEY"
    else:
        variable = "ANTHROPIC_API_KEY" if args.model == "claude-haiku-5-5" else "OPENAI_API_KEY"
    key = os.environ.get(variable, "").strip()
    if not key or not re.fullmatch(r"[!-~]+", key):
        raise AppError(f"Set {variable} for the selected model.")
    return key


def show_draft(args, digest, plan):
    if args.html:
        try:
            export_html(args.html, digest, plan)
        except OSError as error:
            print(f"HTML export skipped ({error.strerror}). The private draft was saved.", file=sys.stderr)
    print(f"Private draft {digest}\n{plan['repo']} #{plan['number']} · {plan['head']} · {plan['model']}")
    if plan["source"] == "fixture":
        print("Offline fixture. Publication disabled.")
    for turn in plan["turns"]:
        print(f"\n[{turn['source']}]\n{turn['body']}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        with closing(Store(args.db)) as store:
            if args.command == "preview":
                args.model = resolve_model(args.model)
                key = model_key(args)
                github = None if args.fixture else connector()
                digest, plan = prepare(store, args.pr, github, args.model, key, groq_smoke=args.groq_smoke)
                show_draft(args, digest, plan)
                return 0
            digest = args.draft if args.command == "show" else args.approve_comments
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise AppError("Supply the complete draft fingerprint from the private preview.")
            plan = store.load(digest)
            if args.command == "show":
                show_draft(args, digest, plan)
                return 0
            if plan["source"] != "connector":
                raise AppError("Fixture drafts cannot be posted to GitHub.")
            receipts = publish(store, digest, plan, connector())
            for turn, comment_id in sorted(receipts.items()):
                print(f"Turn {turn} · https://github.com/{plan['repo']}/pull/{plan['number']}#issuecomment-{comment_id}")
            return 0
    except (AppError, OSError, sqlite3.Error) as error:
        print(f"Commit Issues: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
