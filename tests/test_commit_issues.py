import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commit_issues.debate import (
    FIXTURE_FILES, FIXTURE_TARGET, MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS, ORDER,
    make_turns, model_line, observe, safe_line,
)
from commit_issues.github import AppError, GATEWAY, GitHub
from commit_issues.storage import Store
from commit_issues.workflow import MARKER, export_html, prepare, publish


class FakeConnector:
    def __init__(self):
        self.calls = []
        self.posts = []
        self.comments = []
        self.fail_turn = None
        self.commit_before_failure = False
        self.pr = {
            "number": 1, "state": "open", "draft": False,
            "user": {"type": "User", "login": "demo-person"}, "labels": [{"name": "commit-issues-demo"}],
            "base": {"repo": {"full_name": "demo/leftovers"}, "sha": "a" * 40},
            "head": {"repo": {"full_name": "demo/leftovers"}, "sha": "b" * 40}, "changed_files": 2,
        }

    def send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/pulls/1"):
            return self.pr
        if url.endswith("/pulls/1/files?per_page=12"):
            return FIXTURE_FILES
        if url.endswith("/issues/1/comments?per_page=100"):
            return self.comments[:]
        if method == "POST" and url.endswith("/issues/1/comments"):
            self.posts.append(kwargs["body"]["body"])
            index = len(self.posts)
            row = {"id": index + 100, **kwargs["body"]}
            fail = index == self.fail_turn
            if not fail or self.commit_before_failure:
                self.comments.append(row)
            if fail:
                raise AppError("Simulated network loss.")
            return row
        raise AssertionError(f"Unexpected request {method} {url}")


class CommitIssuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.fake = FakeConnector()
        self.github = GitHub("Demo/Leftovers", send=self.fake.send)

    def preview(self):
        return prepare(self.store, 1, self.github, "mock", None)

    def deliver(self, digest, plan):
        return publish(self.store, digest, plan, self.github, pause=lambda duration: None)

    def test_fixture_runs_all_five_in_order_and_renders_exact_comments(self):
        digest, plan = prepare(self.store, None, None, "mock", None)
        turns = plan["turns"]
        self.assertEqual(tuple(turn["emotion"] for turn in turns), ORDER)
        self.assertEqual([turn["reply_to"] for turn in turns], ["", *ORDER[:-1]])
        self.assertIn("return []", turns[0]["body"])
        self.assertIn("assert", turns[3]["body"])
        self.assertIn("grievance form", turns[-1]["line"])
        path = Path(self.temp.name) / "preview.html"
        export_html(path, digest, plan)
        self.assertIn(digest, path.read_text(encoding="utf-8"))
        self.assertEqual(len(self.fake.calls), 0)
        with self.assertRaises(AppError):
            self.deliver(digest, plan)

    def test_connector_dry_run_and_exact_timeline_writes(self):
        digest, plan = self.preview()
        self.assertEqual([call[0] for call in self.fake.calls], ["GET", "GET", "GET"])
        self.assertEqual(self.store.load(digest), plan)
        receipts = self.deliver(digest, plan)
        self.assertEqual(len(receipts), 7)
        self.assertEqual(len(self.deliver(digest, plan)), 7)
        self.assertEqual(self.fake.posts, [turn["body"] for turn in plan["turns"]])
        for method, url, request in self.fake.calls:
            self.assertTrue(url.startswith(GATEWAY + "/repos/demo/leftovers/"))
            self.assertNotIn("Authorization", request["headers"])
            if method == "POST":
                self.assertEqual(url, GATEWAY + "/repos/demo/leftovers/issues/1/comments")
                self.assertEqual(request["expected"], 201)

    def test_post_does_not_replay_after_database_loss(self):
        digest, plan = self.preview()
        self.deliver(digest, plan)
        fresh = Store(Path(self.temp.name) / "fresh.sqlite3")
        self.addCleanup(fresh.close)
        with self.assertRaisesRegex(AppError, "outside this posting ledger"):
            publish(fresh, fresh.save(plan), plan, self.github)
        self.assertEqual(len(self.fake.posts), 7)

    def test_recovery_recognizes_exact_comment_that_was_written(self):
        digest, plan = self.preview()
        self.fake.fail_turn = 2
        self.fake.commit_before_failure = True
        with self.assertRaises(AppError):
            self.deliver(digest, plan)
        self.assertEqual(self.store.deliveries(digest), {1: 101, 2: None})
        self.assertEqual(len(self.deliver(digest, plan)), 7)
        self.assertEqual(len(self.fake.posts), 7)

    def test_unresolved_write_stops_remaining_comments(self):
        digest, plan = self.preview()
        self.fake.fail_turn = 1
        with self.assertRaises(AppError):
            self.deliver(digest, plan)
        with self.assertRaisesRegex(AppError, "unconfirmed"):
            self.deliver(digest, plan)
        self.assertEqual(len(self.fake.posts), 1)

    def test_concurrent_writer_cannot_reclaim_turn_or_swap_draft(self):
        digest, plan = self.preview()
        self.store.bind_publication(plan, digest)
        self.assertTrue(self.store.claim_turn(digest, 1))
        remote_store = Store(self.db)
        self.addCleanup(remote_store.close)
        with self.assertRaisesRegex(AppError, "unresolved"):
            remote_store.claim_turn(digest, 1)
        with self.assertRaisesRegex(AppError, "Another draft"):
            remote_store.bind_publication(plan, "d" * 64)

    def test_stale_revision_and_removed_opt_in_stop_writes(self):
        digest, plan = self.preview()
        self.fake.pr["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(AppError, "changed"):
            self.deliver(digest, plan)
        self.fake.pr["labels"] = []
        with self.assertRaisesRegex(AppError, "labeled"):
            self.deliver(digest, plan)
        self.assertEqual(self.fake.posts, [])

    def test_midpublication_revision_change_stops_the_remainder(self):
        digest, plan = self.preview()

        def move_head(_):
            self.fake.pr["head"]["sha"] = "c" * 40

        with self.assertRaisesRegex(AppError, "during publication"):
            publish(self.store, digest, plan, self.github, pause=move_head)
        self.assertEqual(len(self.fake.posts), 1)

    def test_rejects_bot_fork_wrong_repo_and_untrusted_gateway(self):
        self.fake.pr["user"]["type"] = "Bot"
        with self.assertRaises(AppError):
            self.preview()
        self.fake.pr["user"]["type"] = "User"
        self.fake.pr["head"]["repo"]["full_name"] = "someone/another-repo"
        with self.assertRaises(AppError):
            self.preview()
        for repo, gateway in [("demo/..", GATEWAY), ("demo/leftovers", GATEWAY + "@redirect.invalid")]:
            with self.assertRaises(AppError):
                GitHub(repo, gateway)
        self.assertEqual(self.fake.posts, [])

    def test_comment_scan_fails_closed(self):
        digest, plan = self.preview()
        self.fake.comments = [{"body": "human comment"}] * 93
        with self.assertRaisesRegex(AppError, "99-comment"):
            self.deliver(digest, plan)
        self.fake.comments = [{"body": f"{MARKER}1 -->", "id": 1}]
        with self.assertRaisesRegex(AppError, "outside this posting ledger"):
            self.deliver(digest, plan)
        self.assertEqual(self.fake.posts, [])

    def test_provider_calls_have_limits_and_no_raw_patch_in_history(self):
        calls = []

        def writer(prompt, context):
            calls.append((prompt, context))
            return "The brackets demand a refund."

        facts = observe(FIXTURE_FILES, 2)
        make_turns(facts, {"Joy": "Anger fought over an assertion."}, writer)
        self.assertEqual(len(calls), 7)
        for prompt, context in calls:
            self.assertLess(len((prompt + str(context)).encode()), MAX_INPUT_BYTES)
            self.assertLessEqual(len(context["previous_two_turns"]), 2)
            self.assertNotIn("leftovers.py", str(context))
            self.assertNotIn("divide_leftovers", str(context))
        self.assertIn("assertion", calls[0][1]["old_grudge"])
        with patch("commit_issues.workflow.model_line", side_effect=AppError("provider unavailable")) as paid:
            _, plan = prepare(self.store, 1, self.github, "gpt-6-luna", "test-key")
            paid.assert_called_once()
            self.assertTrue(all(turn["source"] != "model" for turn in plan["turns"]))

    def test_both_provider_wire_formats_and_unsafe_text(self):
        def sender(method, url, **kwargs):
            self.assertEqual(method, "POST")
            data = kwargs["body"]
            if url.endswith("/messages"):
                self.assertEqual(data["model"], "claude-haiku-5-5")
                self.assertEqual(data["max_tokens"], MAX_OUTPUT_TOKENS)
                self.assertEqual(data["output_config"], {"effort": "low"})
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Hand over the tuba."}]}
            self.assertEqual(data["model"], "gpt-6-luna")
            self.assertEqual(data["max_output_tokens"], MAX_OUTPUT_TOKENS)
            self.assertIs(data["store"], False)
            self.assertEqual(data["reasoning"], {"effort": "none"})
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Hand over the tuba."}]}]}

        for model in ("claude-haiku-5-5", "gpt-6-luna"):
            self.assertEqual(model_line(model, "key", "prompt", {}, send=sender), "Hand over the tuba.")
        for text in ("Hey @contributor", "Click example.com now.", "<img src=x>", "a" * 281):
            self.assertIsNone(safe_line(text))

    def test_budget_is_persistent_and_reservation_is_nonrepeatable(self):
        self.store.reserve_model(FIXTURE_TARGET, "claude-haiku-5-5")
        with self.assertRaisesRegex(AppError, "already has a reservation"):
            self.store.reserve_model(FIXTURE_TARGET, "claude-haiku-5-5")
        for number in (1, 2):
            self.store.reserve_model({**FIXTURE_TARGET, "number": number}, "gpt-6-luna")
        with self.assertRaisesRegex(AppError, "budget is exhausted"):
            self.store.reserve_model({**FIXTURE_TARGET, "number": 3}, "gpt-6-luna")

    def test_cli_requires_paid_approval_and_a_post_fingerprint(self):
        env = dict(os.environ, OPENAI_API_KEY="unused")
        command = [sys.executable, "-m", "commit_issues", "--db", str(self.db)]
        result = subprocess.run([*command, "preview", "--fixture", "--model", "gpt-6-luna"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--approve-paid-model", result.stderr)
        result = subprocess.run([*command, "publish"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--approve-comments", result.stderr)


if __name__ == "__main__":
    unittest.main()
