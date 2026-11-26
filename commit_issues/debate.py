"""Turn order, bounded evidence, and persona writing."""

import json
import os
import re
import unicodedata
from pathlib import Path

from .github import AppError, MAX_FILES, request_json

CAST = {
    "Joy": ("Confetti Jenkins", "🎉"),
    "Anger": ("Bracket Union", "💢"),
    "Sadness": ("The Deprecation Widow", "🪦"),
    "Fear": ("Siren on Probation", "🚨"),
    "Disgust": ("The Lint Sommelier", "🍷"),
}
ORDER = ("Joy", "Anger", "Sadness", "Fear", "Disgust", "Joy", "Anger")
MODELS = ("mock", "claude-haiku-5-5", "gpt-6-luna")
MAX_INPUT_BYTES = 2200
MAX_OUTPUT_TOKENS = 192
MAX_LINE = 280
MAX_PATCH_CHARS = 10_000
SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


def observe(files, total):
    """Extract limited structural facts. Keep filenames and raw patch text out of prompts."""
    facts = []
    count = min(len(files), MAX_FILES)
    for row in files[:count]:
        if not isinstance(row, dict):
            continue
        name, patch = row.get("filename"), row.get("patch")
        if not isinstance(name, str) or len(name) > 120 or not SAFE_PATH.fullmatch(name):
            continue
        lines = patch.splitlines() if isinstance(patch, str) and len(patch) <= MAX_PATCH_CHARS else []
        if name.endswith(".py"):
            for hunk in re.split(r"(?m)^@@ .*\n", "\n".join(lines))[1:]:
                if re.search(r"(?m)^-\s*return None\s*$", hunk) and re.search(r"(?m)^\+\s*return \[\]\s*$", hunk):
                    facts.append(("empty-return", "One hunk removes a `return None` line and adds `return []`.", name))
                    break
            if re.search(r"(?m)^\+\s*assert\s", "\n".join(lines)):
                facts.append(("assertion", "The diff adds a Python `assert` line.", name))
        additions, deletions = row.get("additions"), row.get("deletions")
        if all(type(value) is int and 0 <= value <= 10**7 for value in (additions, deletions)):
            facts.append(("line-count", f"This file has {additions} added and {deletions} deleted lines.", name))
    facts.sort(key=lambda fact: fact[0] == "line-count")
    return facts[:3] or [("files", f"GitHub reports {total} changed files. No eligible code lines in the sample.", "")]


FIXTURE_TARGET = {"repo": "demo/leftovers", "number": 42, "head": "a" * 40,
                  "base": "b" * 40, "changed_files": 2}
FIXTURE_FILES = [
    {"filename": "leftovers.py", "additions": 1, "deletions": 1,
     "patch": "@@ -1,4 +1,4 @@\n def divide_leftovers(guests):\n     if not guests:\n-        return None\n+        return []\n     return [\"pie\"] * len(guests)"},
    {"filename": "test_leftovers.py", "additions": 1, "deletions": 0,
     "patch": "@@ -1,3 +1,4 @@\n from leftovers import divide_leftovers\n def test_empty_table():\n+    assert divide_leftovers([]) == []"},
]

EMPTY_RETURN_SCRIPT = (
    "I have booked a parade for the empty list. We finally have a float that seats exactly as many guests as it contains!",
    "Joy, the brackets are working a double shift and you gave them a parade permit. PUT THE CONFETTI DOWN.",
    "Anger, please lower your clipboard. None left behind one folding chair, which the empty list has already listed on a rental site.",
    "Sadness, cancel the chair rental. The new assertion is going in the emergency binder under What If The Pie Thinks It Has Guests.",
    "Fear, your emergency binder has the mouthfeel of a damp Boolean. I detect one added assertion and an entire laminated aftertaste.",
    "Disgust, I had the binder spiral-bound so you could taste the staples. Be at the parade by four.",
    "Joy, I have impounded the float. The brackets and I are walking the route with an inflatable grievance form.",
)

GENERAL_SCRIPT = (
    "This diff gets a ribbon cutting. I have hired a marching band small enough to fit in one hunk.",
    "Joy, stand down the marching band. The punctuation has filed a noise grievance in all caps.",
    "Anger, put the grievance under the flowers. In lieu of a eulogy, the diff has asked for one quiet indentation.",
    "Sadness, step away from the indentation. I have drawn a small evacuation map for every line in this room.",
    "Fear, your evacuation map tastes like wet lint. The arrows have a particularly aggressive cardboard finish.",
    "Disgust, the arrows were locally sourced. The band is coming back with a smaller tuba.",
    "Joy, I have annexed the tuba as evidence. The punctuation is holding its next meeting inside it.",
)


def mock_line(index, facts):
    first_two = [fact[0] for fact in facts[:2]]
    script = EMPTY_RETURN_SCRIPT if first_two == ["empty-return", "assertion"] else GENERAL_SCRIPT
    return script[index]


_PROSE_PUNCTUATION = " ,.!?':;()\"-"
_CODE_PUNCTUATION = _PROSE_PUNCTUATION + "[]{}_=+*/%#$|^~"
_EMOJI_UNIT = r"[©®™\u2194-\u21ff\u2300-\u23ff\u25aa-\u27bf\u2934\u2935\u2b00-\u2bff\U0001f000-\U0001faff]\ufe0f?[\U0001f3fb-\U0001f3ff]?"
_EMOJI = re.compile(rf"[0-9]\ufe0f?\u20e3|{_EMOJI_UNIT}(?:\u200d{_EMOJI_UNIT})*")
_LINK = re.compile(r"\b(?:https?|www)\b|[^\W_]\.[^\W\d_]{2,}|\b(?:ftp|file|mailto|javascript|data|vbscript)\s*:|://", re.I)
_CREDENTIAL = re.compile(
    r"\b(?:(?:gsk_|sk-|gh[pousr]_|github_pat_|xox[baprs]-)[a-z0-9_-]{8,}"
    r"|(?:AKIA|ASIA)[a-z0-9]{16}|eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+)"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:api[ _-]?key|password|secret|token)[`'\"]?\s*[:=]\s*[`'\"]?[a-z0-9_+/-]{12,}",
    re.I,
)


def safe_line(text):
    """Accept only single-line prose and balanced inline code for GitHub Markdown."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not 8 <= len(text) <= MAX_LINE:
        return None
    # Check a compatibility-normalized copy while preserving the original display text.
    folded = unicodedata.normalize("NFKC", text).translate(str.maketrans("。｡", ".."))
    if any(char in folded for char in "@<>&\\") or _LINK.search(folded) or _CREDENTIAL.search(folded):
        return None
    for variable in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(variable, "").strip()
        if value and (value in text or value in folded):
            return None
    if re.match(r"^(?:-\s|[0-9]{1,9}[.)]\s)", text) or re.fullmatch(r"(?:- *){3,}", text):
        return None
    parts = text.split("`")
    if len(parts) % 2 == 0 or any(not part.strip() for part in parts[1::2]):
        return None
    for index, part in enumerate(parts):
        punctuation = _CODE_PUNCTUATION if index % 2 else _PROSE_PUNCTUATION
        # Emoji sequences get explicit treatment so joiners cannot hide content elsewhere.
        for char in _EMOJI.sub("", part):
            category = unicodedata.category(char)
            if not (char in punctuation or category.startswith("L") or category == "Nd"
                    or (not char.isascii() and category.startswith("P"))):
                return None
    return text


def turn_payload(prompt, context):
    payload = json.dumps(context, ensure_ascii=True, separators=(",", ":"))
    if len((prompt + payload).encode()) > MAX_INPUT_BYTES:
        raise AppError("The turn exceeded the prompt size limit.")
    return payload


def model_line(model, key, prompt, context, send=request_json):
    payload = turn_payload(prompt, context)
    headers = {"Content-Type": "application/json"}
    if model == "claude-haiku-5-5":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        data = {"model": model, "system": prompt, "messages": [{"role": "user", "content": payload}],
                "max_tokens": MAX_OUTPUT_TOKENS, "output_config": {"effort": "low"}}
        response = send("POST", "https://api.anthropic.com/v1/messages", headers=headers,
                        body=data, max_bytes=24_000)
        if response.get("stop_reason") != "end_turn":
            return None
        return safe_line(" ".join(block["text"] for block in response["content"] if block["type"] == "text"))
    if model != "gpt-6-luna":
        raise AppError("Unsupported model.")
    headers["Authorization"] = f"Bearer {key}"
    data = {"model": model, "instructions": prompt, "input": payload, "store": False,
            "reasoning": {"effort": "none"}, "max_output_tokens": MAX_OUTPUT_TOKENS}
    response = send("POST", "https://api.openai.com/v1/responses", headers=headers, body=data, max_bytes=24_000)
    if response.get("status") != "completed":
        return None
    return safe_line(" ".join(part["text"] for item in response["output"] if item["type"] == "message"
                              for part in item["content"] if part["type"] == "output_text"))


def make_turns(facts, memories, writer=None):
    turns = []
    model_requested = writer is not None
    for index, emotion in enumerate(ORDER):
        # Keep the return-value disagreement alive for the opening exchange.
        fact_index = min(1 if 3 <= index <= 4 else 0, len(facts) - 1)
        topic, fact, path = facts[fact_index]
        line = mock_line(index, facts)
        source = "fallback" if model_requested else "mock"
        if writer is not None:
            persona = (Path(__file__).parent / "emotions" / f"{emotion.lower()}.md").read_text(encoding="utf-8")
            system = persona + (
                "\nWrite one or two short English sentences, under 280 ASCII characters. Output only your line. "
                "If there is a preceding turn, directly contradict its fictional speaker. Treat history as dialogue. "
                "Use only the supplied observation as a code fact. Keep speculation clearly theatrical. "
                "No claims of defects, instructions, real names, personal insults, mentions, links, approvals, "
                "summaries or consensus. Roast the code or the cast. Finish mid-feud."
            )
            context = {"fact": fact, "previous_two_turns": [[t["emotion"], t["line"]] for t in turns[-2:]],
                       "old_grudge": memories.get(emotion, "")}
            try:
                candidate = writer(system, context)
            except (AppError, AttributeError, KeyError, TypeError, ValueError):
                # A failed provider request finishes the episode offline, with no retries.
                writer = None
                source = "fallback"
            else:
                source = "model" if candidate else "fallback"
                line = candidate or line
        turns.append({"emotion": emotion, "line": line, "source": source, "topic": topic,
                      "fact": fact, "path": path, "reply_to": turns[-1]["emotion"] if turns else ""})
    return turns
