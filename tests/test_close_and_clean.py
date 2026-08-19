import datetime
import json
import os
import pathlib
import tempfile
import unittest

from helpers import commit_all, init_repo, launcher, write
from tuicr_round.protocol import HEADER_PREFIX
from tuicr_round.state import prepare_root, save_round
from tuicr_round.util import isoformat, utc_now


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("tuicr 999.0.0-test")
elif args[:2] == ["review", "list"]:
    print(json.dumps([{"slug":"fixture/worktree","path":str(pathlib.Path(os.environ["HOME"])/"session.json"),"active":False}]))
elif args[:2] == ["review", "comments"]:
    print(pathlib.Path(os.environ["FAKE_COMMENTS"]).read_text())
else:
    raise SystemExit(5)
'''


class CloseAndCleanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.repo = init_repo(self.base / "repo")
        write(self.repo / "file", "base\n")
        commit_all(self.repo)
        write(self.repo / "file", "dirty\n")
        self.state = self.base / "state"
        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.round_id = payload["round"]
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        write(fake_bin / "tuicr", FAKE, 0o755)
        # A no-session tmux makes active-TUI checks deterministic.
        write(fake_bin / "tmux", "#!/bin/sh\nexit 1\n", 0o755)
        self.comments = self.base / "comments.json"
        self.env = {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "FAKE_COMMENTS": str(self.comments),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _comment(self, comment_id, status, reply_to=None):
        metadata = {
            "version": 1,
            "role": "human",
            "author": "Fixture User",
            "severity": "warning",
            "status": status,
            "reply_to": reply_to,
        }
        return {"id": comment_id, "content": HEADER_PREFIX + json.dumps(metadata, separators=(",", ":")) + "\nmessage"}

    def test_unresolved_and_unstructured_comments_require_confirmation(self):
        self.comments.write_text(json.dumps([{"id": "legacy", "content": "plain comment"}]))
        result, payload = launcher(self.state, "close", "--round", self.round_id, extra_env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.assertEqual(payload["error"]["details"]["unstructured"], 1)

        self.comments.write_text(json.dumps([self._comment("root", "open")]))
        result, payload = launcher(self.state, "close", "--round", self.round_id, extra_env=self.env)
        self.assertEqual(payload["error"]["details"]["blocking_threads"], ["root"])

    def test_close_token_is_bound_to_current_comment_digest(self):
        comments = [self._comment("root", "open")]
        self.comments.write_text(json.dumps(comments))
        result, payload = launcher(self.state, "close", "--round", self.round_id, extra_env=self.env)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        token = payload["error"]["details"]["token"]

        comments.append(self._comment("new-root", "reject"))
        self.comments.write_text(json.dumps(comments))
        result, payload = launcher(self.state, "close", "--round", self.round_id, "--confirm", token, extra_env=self.env)
        self.assertEqual(payload["error"]["code"], "stale_confirmation")

        result, payload = launcher(self.state, "close", "--round", self.round_id, extra_env=self.env)
        fresh = payload["error"]["details"]["token"]
        result, payload = launcher(self.state, "close", "--round", self.round_id, "--confirm", fresh, extra_env=self.env)
        self.assertEqual(result.returncode, 0, payload)
        self.assertIsNotNone(payload["closed_at"])

    def test_resolved_comments_close_without_confirmation(self):
        comments = [self._comment("root", "open"), self._comment("reply", "accept", "root")]
        self.comments.write_text(json.dumps(comments))
        result, payload = launcher(self.state, "close", "--round", self.round_id, extra_env=self.env)
        self.assertEqual(result.returncode, 0, payload)

    def test_accepted_returns_only_latest_accept_threads(self):
        comments = [
            self._comment("one", "open"), self._comment("one-a", "accept", "one"),
            self._comment("two", "open"), self._comment("two-d", "discuss", "two"),
        ]
        self.comments.write_text(json.dumps(comments))
        result, payload = launcher(self.state, "accepted", "--round", self.round_id, extra_env=self.env)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual([item["root_id"] for item in payload["accepted"]], ["one"])

    def test_clean_only_removes_old_closed_inactive_rounds(self):
        clean_state = self.base / "clean-state"
        root = prepare_root(clean_state)
        old = isoformat(utc_now() - datetime.timedelta(days=31))
        recent = isoformat(utc_now() - datetime.timedelta(days=1))

        def make(round_id, closed_at, owner_pid=None):
            destination = root / "rounds" / round_id
            destination.mkdir(mode=0o700)
            (destination / "git" / "tmux").mkdir(mode=0o700, parents=True)
            value = {
                "id": round_id,
                "repo_root": str(self.repo),
                "closed_at": closed_at,
                "owner_pid": owner_pid,
                "external_git_dir": str(destination / "git"),
            }
            save_round(root, value)

        make("00000000-0000-4000-8000-000000000001", old)
        make("00000000-0000-4000-8000-000000000002", None)
        make("00000000-0000-4000-8000-000000000003", recent)
        make("00000000-0000-4000-8000-000000000004", old, os.getpid())
        result, payload = launcher(clean_state, "clean", extra_env=self.env)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["removed"], ["00000000-0000-4000-8000-000000000001"])
        self.assertTrue((root / "rounds" / "00000000-0000-4000-8000-000000000002").exists())
        self.assertTrue((root / "rounds" / "00000000-0000-4000-8000-000000000003").exists())
        self.assertTrue((root / "rounds" / "00000000-0000-4000-8000-000000000004").exists())


if __name__ == "__main__":
    unittest.main()
