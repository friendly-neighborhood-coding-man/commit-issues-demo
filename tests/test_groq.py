import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from groq import Groq

from commit_issues.__main__ import main, resolve_model
from commit_issues.debate import EMPTY_RETURN_SCRIPT, FIXTURE_FILES, FIXTURE_TARGET, ORDER, make_turns, observe, safe_line
from commit_issues.github import AppError, HTTPStatusError
from commit_issues.groq import DEFAULT_GROQ_MODEL, GROQ_BASE_URL, GROQ_TIMEOUT_SECONDS, GroqWriter
from commit_issues.storage import Store
from commit_issues.workflow import prepare

GPT_OSS = "openai/gpt-oss-20b"
TRACE = "Secret thought revealed."
FAKE_KEY = "test-key-only"


def completion(line="Return the confetti cannon.", *, finish="stop", role="assistant"):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": GPT_OSS,
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
            "choices": [{"index": 0, "finish_reason": finish, "message": {
        "role": role, "content": line, "reasoning": TRACE, "reasoning_content": TRACE,
    }}]}


@contextmanager
def mock_wire(*outcomes):
    send = Mock(side_effect=outcomes)

    def respond(request):
        result = send(request)
        return httpx.Response(200, json=result) if isinstance(result, dict) else result

    def client(http_client, **options):
        http_client.close()
        return Groq(http_client=httpx.Client(transport=httpx.MockTransport(respond),
                                            follow_redirects=http_client.follow_redirects), **options)

    with patch("commit_issues.groq.Groq", side_effect=client) as factory:
        yield send, factory


def rate_limit(wait):
    headers = {} if wait is None else {"Retry-After": wait}
    return httpx.Response(429, headers=headers, json={"error": {"message": "Rate limited"}})


class GroqTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_configuration_defaults_switches_and_explicit_overrides(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": FAKE_KEY, "GROQ_MODEL": GPT_OSS}, clear=True):
            self.assertEqual(resolve_model(None), "mock")
        with patch.dict(os.environ, {"AI_PROVIDER": "groq"}, clear=True):
            self.assertEqual(resolve_model(None), DEFAULT_GROQ_MODEL)
            os.environ["GROQ_MODEL"] = GPT_OSS
            self.assertEqual(resolve_model(None), GPT_OSS)
            os.environ["GROQ_MODEL"] = FAKE_KEY
            with self.assertRaisesRegex(AppError, "GROQ_MODEL"):
                resolve_model(None)
            for model in ("mock", "claude-haiku-5-5", "gpt-6-luna", DEFAULT_GROQ_MODEL):
                self.assertEqual(resolve_model(model), model)

    def test_cli_paid_gate_and_missing_key_are_checked_before_network(self):
        args = ["--db", str(self.path), "preview", "--fixture"]
        with patch.dict(os.environ, {"AI_PROVIDER": "groq", "GROQ_API_KEY": FAKE_KEY}, clear=True), \
             patch("commit_issues.groq.Groq") as factory, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(args), 1)
            self.assertIn("--approve-paid-model", stderr.getvalue())
            os.environ.pop("GROQ_API_KEY")
            self.assertEqual(main([*args, "--approve-paid-model"]), 1)
            self.assertIn("GROQ_API_KEY", stderr.getvalue())
            self.assertNotIn(FAKE_KEY, stderr.getvalue())
        factory.assert_not_called()

    def test_wire_parameters_and_existing_validation(self):
        for model, effort, temperature, max_tokens in (
            (DEFAULT_GROQ_MODEL, "none", 0.9, 512), (GPT_OSS, "low", 0.7, 1024),
        ):
            with self.subTest(model=model), mock_wire(completion()) as (send, factory):
                context = {"fact": "The diff adds an assert.", "previous_two_turns": [["Joy", "Throw confetti."]],
                           "old_grudge": "Anger argued about assertion."}
                writer = GroqWriter(model, FAKE_KEY)
                self.assertEqual(writer("Persona prompt", context), "Return the confetti cannon.")
                self.assertEqual(writer.usage.total_tokens, 13)
                factory.assert_called_once()
                settings = dict(factory.call_args.kwargs)
                self.assertFalse(settings.pop("http_client").follow_redirects)
                self.assertEqual(settings, {"api_key": FAKE_KEY, "base_url": GROQ_BASE_URL, "max_retries": 0,
                                            "timeout": GROQ_TIMEOUT_SECONDS})
                request = send.call_args.args[0]
                self.assertEqual((request.method, str(request.url)), ("POST", GROQ_BASE_URL + "/openai/v1/chat/completions"))
                self.assertEqual(request.headers["Authorization"], f"Bearer {FAKE_KEY}")
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["reasoning_effort"], effort)
                self.assertEqual(payload["temperature"], temperature)
                self.assertEqual(payload["max_completion_tokens"], max_tokens)
                self.assertNotIn("reasoning_format", payload)
                self.assertEqual(payload["messages"][0], {"role": "system", "content": "Persona prompt"})
                self.assertEqual(json.loads(payload["messages"][1]["content"]), context)
                self.assertNotIn(FAKE_KEY, json.dumps(payload))
                if model == GPT_OSS:
                    self.assertIs(payload["include_reasoning"], False)

    def test_seven_turns_reuse_exact_prompt_context_and_comment_format(self):
        turns_sent = []

        def scripted(prompt, context):
            turns_sent.append((prompt, context))
            return EMPTY_RETURN_SCRIPT[len(turns_sent) - 1]

        memories = {"Fear": "Sadness argued about assertion."}
        with patch.object(self.store, "memories", return_value=memories), \
             mock_wire(*map(completion, EMPTY_RETURN_SCRIPT)) as (send, _):
            digest, plan = prepare(self.store, None, None, GPT_OSS, FAKE_KEY)
        make_turns(observe(FIXTURE_FILES, 2), memories, scripted)
        self.assertEqual(send.call_count, len(ORDER))
        for call, (prompt, context) in zip(send.call_args_list, turns_sent):
            system_message, user_message = json.loads(call.args[0].content)["messages"]
            self.assertEqual(system_message["content"], prompt)
            self.assertEqual(json.loads(user_message["content"]), context)
            self.assertNotIn(TRACE, user_message["content"])
        _, mock_plan = prepare(self.store, None, None, "mock", None)
        self.assertEqual(tuple(turn["emotion"] for turn in plan["turns"]), ORDER)
        self.assertEqual([turn["source"] for turn in plan["turns"]], ["model"] * 7)
        self.assertEqual([turn["body"] for turn in plan["turns"]], [turn["body"] for turn in mock_plan["turns"]])
        self.assertNotIn(TRACE, json.dumps(self.store.load(digest)))

    def test_retry_after_uses_same_turn_and_existing_reservation(self):
        with mock_wire(rate_limit("2"), *[completion() for _ in range(6)]) as (send, _), \
                patch("commit_issues.groq.sleep") as sleep:
            _, plan = prepare(self.store, None, None, DEFAULT_GROQ_MODEL, FAKE_KEY)
        sleep.assert_called_once_with(2)
        self.assertEqual(send.call_count, 7)
        self.assertEqual(send.call_args_list[0].args[0].content, send.call_args_list[1].args[0].content)
        self.assertEqual([turn["source"] for turn in plan["turns"]], ["model"] * 6 + ["fallback"])
        self.assertEqual(self.store.db.execute("SELECT calls FROM model_reservations").fetchone(), (7,))
        self.store.reserve_model(FIXTURE_TARGET, GPT_OSS)
        self.store.reserve_model(FIXTURE_TARGET, "claude-haiku-5-5")
        with self.assertRaisesRegex(AppError, "budget is exhausted"):
            self.store.reserve_model(FIXTURE_TARGET, "gpt-6-luna")

    def test_rate_limit_retries_are_bounded(self):
        for wait in (None, "invalid", "120", "nan", "-1"):
            with self.subTest(wait=wait), patch("commit_issues.groq.sleep") as sleep, \
                 mock_wire(rate_limit(wait)) as (send, _):
                with self.assertRaises(HTTPStatusError):
                    GroqWriter(DEFAULT_GROQ_MODEL, FAKE_KEY)("prompt", {})
                sleep.assert_not_called()
                send.assert_called_once()
        with patch("commit_issues.groq.sleep") as sleep, \
             mock_wire(rate_limit("1"), rate_limit("1")) as (send, _):
            with self.assertRaises(HTTPStatusError):
                GroqWriter(DEFAULT_GROQ_MODEL, FAKE_KEY)("prompt", {})
            sleep.assert_called_once_with(1)
            self.assertEqual(send.call_count, 2)

    def test_timeout_and_bad_json_fall_back_without_other_providers(self):
        for index, error in enumerate((httpx.ReadTimeout(FAKE_KEY), b'{"choices":', b'{"choices": []}')):
            with self.subTest(error_type=type(error).__name__):
                outcome = (httpx.Response(200, headers={"Content-Type": "application/json"}, content=error)
                           if isinstance(error, bytes) else error)
                with patch("commit_issues.workflow.FIXTURE_TARGET", {**FIXTURE_TARGET, "number": index + 1}), \
                     mock_wire(outcome) as (send, _), \
                     patch("commit_issues.workflow.model_line") as alternate:
                    _, plan = prepare(self.store, None, None, GPT_OSS, FAKE_KEY)
                alternate.assert_not_called()
                send.assert_called_once()
                self.assertEqual([turn["source"] for turn in plan["turns"]], ["fallback"] * 7)
                self.assertNotIn(FAKE_KEY, json.dumps(plan))

    def test_truncated_unsafe_or_reasoning_only_content_is_rejected(self):
        for result in (completion(finish="length"), completion(role="tool"), completion(None),
                       completion(f"<think>{TRACE}</think>"), completion("Visit @someone now.")):
            with mock_wire(result):
                self.assertIsNone(GroqWriter(GPT_OSS, FAKE_KEY)("prompt", {}))

    def test_original_gpt_oss_smoke_reply_survives_validation_and_rendering(self):
        reply = ("Welcome to the grand ribbon‑cutting of your code, dear dev! We’ve swapped that pesky `return None` "
                 "for a shiny `return []`, turning empty returns into a parade of empty lists—cheers to the new, "
                 "never‑empty future!")
        with mock_wire(completion(reply)):
            digest, plan = prepare(self.store, None, None, GPT_OSS, FAKE_KEY, groq_smoke=True)
        turn = self.store.load(digest)["turns"][0]
        self.assertEqual(turn["source"], "model")
        self.assertEqual(turn["line"], reply)
        self.assertIn(f"\n\n{reply}\n\n> On the diff · `leftovers.py`", turn["body"])
        self.assertIn("<!-- commit-issues:v0:turn:1 -->", plan["turns"][0]["body"])

    def test_expressive_reactions_do_not_admit_other_markdown_or_hidden_content(self):
        for text in ("Don’t TOUCH `int(items[0])`—I’M CALLING THE PARADE!!! 🎉",
                     "We’ve got 👩🏽‍🚒 on standby and 1️⃣ alarm!!!", "It’s café time. 🍷", "A" * 280):
            with self.subTest(text=text):
                self.assertEqual(safe_line(text), text)
        for text in ("A" * 281, "Too few", "Confetti for @octocat!", "Notify ＠octocat now!",
                     "Try `@octocat` instead!", "Use &#64;octocat now!", "Here <!-- hidden -->!",
                     "Here <details>hidden</details>!", "Try ＆lt;hidden markup!", "Confetti\u202eright now!",
                     "Con\u200dfetti right now!", "Done now!\nHide this line!", "Go to www.example.com now!",
                     "Try evil。example now!", "Try https：//evil.example now!", "Open [this](javascript:alert) now!",
                     "Try [this](#other-section) instead!", "Use ```touch demo``` now!", "Unclosed `return []",
                     "A blank ` ` is enough!", "- First draft item!", "1. First draft item!", "---------",
                     "Emphasis with *️⃣hidden*️⃣ isn't inline code!",
                     "Use `gsk_testsecret0123456789` now!", "The password is `sk-ant-testsecret1234567`!",
                     "Use api_key=`github_pat_testsecret0123456789`!", "Use `password=longsecretvalue`!",
                     "Our secret is now set. `secret` = `mysteryvalue01234`",
                     "Try `ghp_testsecret0123456789` now!"):
            with self.subTest(text=text):
                self.assertIsNone(safe_line(text))
        with patch.dict(os.environ, {"GROQ_API_KEY": FAKE_KEY}):
            self.assertIsNone(safe_line(f"Yell `{FAKE_KEY}` at the parade!"))

    def test_smoke_uses_one_call_per_model_and_requires_the_fixture(self):
        for model in (DEFAULT_GROQ_MODEL, GPT_OSS):
            args = ["--db", str(self.path), "preview", "--fixture", "--groq-smoke", "--approve-paid-model"]
            with patch.dict(os.environ, {"AI_PROVIDER": "groq", "GROQ_MODEL": model, "GROQ_API_KEY": FAKE_KEY}), \
                 mock_wire(completion()) as (send, _), \
                 redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args), 0)
            send.assert_called_once()
            self.assertEqual(json.loads(send.call_args.args[0].content)["model"], model)
            self.assertIn("Offline fixture. Publication disabled.", stdout.getvalue())
            self.assertEqual(stdout.getvalue().count("[model]"), 1)
            self.assertEqual(stdout.getvalue().count("[fallback]"), 6)
        with self.assertRaisesRegex(AppError, "requires --fixture"):
            prepare(self.store, 1, Mock(), GPT_OSS, FAKE_KEY, groq_smoke=True)
        with mock_wire(rate_limit("1")) as (send, _):
            with self.assertRaises(HTTPStatusError):
                GroqWriter(GPT_OSS, FAKE_KEY, smoke=True)("prompt", {})
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
