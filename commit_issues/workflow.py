"""Private preview and publishing of a fingerprint-approved transcript."""

import os
import re
import time
from html import escape
from pathlib import Path

from .debate import CAST, FIXTURE_FILES, FIXTURE_TARGET, ORDER, make_turns, model_line, observe
from .github import AppError
from .groq import GROQ_MODELS, GroqWriter

MARKER = "<!-- commit-issues:v0:turn:"
PAUSE_SECONDS = 1.2


def prepare(store, number, github, model, key, *, groq_smoke=False):
    if groq_smoke and (github is not None or model not in GROQ_MODELS):
        raise AppError("--groq-smoke requires --fixture and a Groq model.")
    if github is None:
        target, files = FIXTURE_TARGET, FIXTURE_FILES
    else:
        target = github.target(number)
        files = github.files(number)
        if github.target(number) != target:
            raise AppError("The PR changed while collecting evidence. Run a fresh preview.")
    facts = observe(files, target["changed_files"])
    writer = None
    if model != "mock":
        store.reserve_model(target, model)

        def writer(prompt, context):
            return model_line(model, key, prompt, context)

        if model in GROQ_MODELS:
            writer = GroqWriter(model, key, smoke=groq_smoke)

    turns = make_turns(facts, store.memories(target), writer)
    for index, turn in enumerate(turns, 1):
        name, emoji = CAST[turn["emotion"]]
        reply = f" · Re: {turn['reply_to']}" if turn["reply_to"] else ""
        path = f"`{turn['path']}` · " if turn["path"] else ""
        turn["body"] = (
            f"**{emoji} {turn['emotion']} · {name}**  \n_Commit Issues · {index}/{len(ORDER)}{reply}_\n\n"
            f"{turn['line']}\n\n> On the diff · {path}{turn['fact']}\n\n{MARKER}{index} -->"
        )
    plan = {**target, "source": "fixture" if github is None else "connector", "model": model, "turns": turns}
    return store.save(plan), plan


def publish(store, digest, plan, github, pause=time.sleep):
    if plan["source"] != "connector" or plan["repo"] != github.repo:
        raise AppError("Only a Connector preview for the configured demo repository can be published.")
    number = plan["number"]
    turns = plan["turns"]
    if len(turns) != len(ORDER):
        raise AppError("The draft does not have the expected turn count.")
    expected_target = {field: plan[field] for field in ("repo", "number", "head", "base", "changed_files")}
    if github.target(number) != expected_target:
        raise AppError("The approved PR revision has changed. Create a fresh preview.")
    existing = github.comments(number)
    ledger = store.deliveries(digest)
    remote_turns = {}
    for comment in existing:
        if MARKER not in comment["body"]:
            continue
        match = re.search(re.escape(MARKER) + r"([1-7]) -->\Z", comment["body"])
        index = int(match[1]) if match else 0
        comment_id = comment.get("id")
        if (index not in ledger or index in remote_turns or comment["body"] != turns[index - 1]["body"]
                or type(comment_id) is not int or comment_id < 1 or ledger[index] not in (None, comment_id)):
            raise AppError("Found an existing Commit Issues episode outside this posting ledger.")
        remote_turns[index] = comment_id
    if any(index not in remote_turns for index in ledger):
        raise AppError("A previous write is missing or unconfirmed on GitHub. No duplicate POST was sent.")
    if len(existing) + len(turns) - len(ledger) >= 100:
        raise AppError("Publishing would exceed the 99-comment history limit.")
    store.bind_publication(plan, digest)
    for index, comment_id in remote_turns.items():
        store.confirm_turn(digest, index, comment_id)
    for index, turn in enumerate(turns, 1):
        if index in ledger:
            continue
        if github.target(number) != expected_target:
            raise AppError("The PR changed during publication. Remaining turns were stopped.")
        if not store.claim_turn(digest, index):
            continue
        try:
            comment_id = github.post(number, turn["body"])
        except AppError as error:
            raise AppError(f"Turn {index} stopped publication. {error} Inspect the PR before resuming.") from None
        store.confirm_turn(digest, index, comment_id)
        if index < len(turns):
            pause(PAUSE_SECONDS)
    return store.deliveries(digest)


def export_html(filename, digest, plan):
    cards = []
    for turn in plan["turns"]:
        name, emoji = CAST[turn["emotion"]]
        reply = f'Re: {turn["reply_to"]} · ' if turn["reply_to"] else ""
        evidence = f'{turn["path"]} · ' if turn["path"] else ""
        cards.append(f'<article class="{turn["emotion"].lower()}"><h2>{emoji} {escape(name)}</h2>'
                     f'<small>{escape(reply + turn["emotion"] + " · " + turn["source"])}</small>'
                     f'<p>{escape(turn["line"])}</p><aside>{escape(evidence + turn["fact"].replace("`", ""))}</aside>'
                     f'<details><summary>Exact comment Markdown</summary><pre>{escape(turn["body"])}</pre></details></article>')
    target = escape(f'{plan["repo"]} #{plan["number"]} · {plan["head"][:8]} · {plan["model"]}')
    label = "Offline fixture" if plan["source"] == "fixture" else "Private draft"
    document = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commit Issues · {label}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;color:#f6eddd;background:#1c1b22;font:16px/1.5 system-ui,sans-serif}}
main{{max-width:49rem;margin:auto;padding:3rem clamp(1rem,5vw,3rem)}} h1{{margin:.2rem 0;font-size:clamp(2.8rem,9vw,5.4rem);letter-spacing:-.075em;line-height:1}}
header p{{color:#dabf9f;margin:0 0 1.8rem}} article{{border-left:.28rem solid var(--accent);padding:1.1rem 1.4rem;background:#2b2831;margin:.75rem 0;border-radius:.35rem}}
.joy{{--accent:#f4ca65}} .anger{{--accent:#ff8a7a}} .sadness{{--accent:#b9aeff}} .fear{{--accent:#94cfff}} .disgust{{--accent:#b8db81}}
h2{{font-size:1.24rem;line-height:1.2;margin:0;color:var(--accent)}} small,aside,summary{{color:#ddcdbf;font-size:.88rem}} article p{{margin:.5rem 0}}
summary{{cursor:pointer;margin-top:.55rem}} pre{{white-space:pre-wrap;overflow-wrap:anywhere}} footer{{color:#ddcdbf;overflow-wrap:anywhere}}
</style><main><header><small>{label}</small><h1>Commit Issues</h1><p>{target}</p></header>
{''.join(cards)}<footer><p>Draft fingerprint <code>{digest}</code></p></footer></main></html>'''
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
        output.write(document)
