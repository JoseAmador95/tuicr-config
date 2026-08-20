import concurrent.futures
import json
import os
import pathlib
import tempfile
import textwrap
import unittest
from unittest import mock

from helpers import commit_all, init_repo, launcher, write
from tuicr_round import protocol
from tuicr_round.util import RoundError


FAKE_TUICR = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("tuicr 999.0.0-test")
    raise SystemExit(0)
home = pathlib.Path(os.environ["HOME"])
session = home / "review-session.json"
if args[:2] == ["review", "list"]:
    print(json.dumps([{"slug":"fixture/worktree","kind":"local","path":str(session),"active":False}]))
elif args[:2] == ["review", "comments"]:
    source = os.environ.get("FAKE_COMMENTS")
    print(pathlib.Path(source).read_text() if source else "[]")
elif args[:2] == ["review", "add"]:
    active = home / "fake-active"
    overlap = home / "fake-overlap"
    try:
        descriptor = os.open(str(active), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        overlap.write_text("yes")
        descriptor = None
    payload = json.load(sys.stdin)
    time.sleep(0.08)
    with (home / "adds.jsonl").open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    if descriptor is not None:
        os.close(descriptor)
        active.unlink()
    print(json.dumps({"id":"added","content":payload["content"]}))
else:
    print(json.dumps({"error":"unexpected", "args":args}))
    raise SystemExit(4)
'''


FAKE_TMUX = r'''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
socket = pathlib.Path(args[args.index("-S") + 1])
root = socket.parent
root.mkdir(parents=True, exist_ok=True)
marker = root / "fake-session"
log = root / "fake-tmux.log"
command = args[args.index("/dev/null") + 1:]
with log.open("a") as handle:
    handle.write(" ".join(command) + "\n")
if command and command[0] == "has-session":
    raise SystemExit(0 if marker.exists() else 1)
if command and command[0] == "new-session":
    marker.write_text("active")
elif command and command[0] == "display-message":
    print(os.getpid() if command[-1] == "#{pid}" else "0")
raise SystemExit(0)
'''


class TuicrAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bin = pathlib.Path(self.temporary.name) / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_arbitrary_version_output_is_accepted(self):
        write(
            self.bin / "tuicr",
            '#!/bin/sh\ntest "$#" -eq 1 && test "$1" = "--version" || exit 97\nprintf \'custom nightly build\\n\'\n',
            0o755,
        )
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertIsNone(protocol.check_tuicr_available())

    def test_missing_executable_is_structured(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            with self.assertRaises(RoundError) as raised:
                protocol.check_tuicr_available()
        self.assertEqual(raised.exception.code, "missing_dependency")
        self.assertEqual(raised.exception.details, {"executable": "tuicr"})
        self.assertEqual(raised.exception.exit_code, 2)

    def test_nonzero_version_probe_is_structured(self):
        write(self.bin / "tuicr", "#!/bin/sh\nprintf 'broken probe\\n' >&2\nexit 19\n", 0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            with self.assertRaises(RoundError) as raised:
                protocol.check_tuicr_available()
        self.assertEqual(raised.exception.code, "command_failed")
        self.assertEqual(
            raised.exception.details,
            {"argv": ["tuicr", "--version"], "exit_code": 19, "stderr": "broken probe"},
        )
        self.assertEqual(raised.exception.exit_code, 2)


class CliProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.repo = init_repo(self.base / "repo")
        write(self.repo / "file.txt", "base\n")
        commit_all(self.repo)
        write(self.repo / "file.txt", "dirty\n")
        self.state = self.base / "state"
        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.start_payload = payload
        self.round_id = payload["round"]
        self.home = self.state / "rounds" / self.round_id / "home"
        self.fake_bin = self.base / "fake-bin"
        self.fake_bin.mkdir()
        write(self.fake_bin / "tuicr", FAKE_TUICR, 0o755)
        write(self.fake_bin / "tmux", FAKE_TMUX, 0o755)
        self.fake_env = {"PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_errors_are_json_and_ambiguity_fails_closed(self):
        result, payload = launcher(self.state, "status", extra_env=self.fake_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        second, second_payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(second.returncode, 0, second_payload)
        result, payload = launcher(self.state, "status", "--repo", self.repo, extra_env=self.fake_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "ambiguous_round")

    def test_concurrent_add_is_serialized_and_header_is_exact(self):
        arguments = (
            "add", "--round", self.round_id, "--role", "agent", "--author", "Agent 7",
            "--severity", "blocker", "--path", "file.txt", "--start", "1", "finding",
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: launcher(self.state, *arguments, extra_env=self.fake_env), range(2)))
        for result, payload in results:
            self.assertEqual(result.returncode, 0, payload)
        self.assertFalse((self.home / "fake-overlap").exists())
        payloads = [json.loads(line) for line in (self.home / "adds.jsonl").read_text().splitlines()]
        self.assertEqual(len(payloads), 2)
        first_line = payloads[0]["content"].splitlines()[0]
        self.assertEqual(
            first_line,
            '@nvim-review {"version":1,"role":"agent","author":"Agent 7","severity":"blocker","status":"open","reply_to":null}',
        )
        self.assertEqual(payloads[0]["type"], "issue")

    def test_agent_author_and_target_validation(self):
        result, payload = launcher(
            self.state, "add", "--round", self.round_id, "--role", "agent",
            "--severity", "warning", "message", extra_env=self.fake_env,
        )
        self.assertEqual(payload["error"]["code"], "author_required")
        result, payload = launcher(
            self.state, "add", "--round", self.round_id, "--severity", "warning",
            "--path", "../escape", "message", extra_env=self.fake_env,
        )
        self.assertEqual(payload["error"]["code"], "invalid_target")
        result, payload = launcher(
            self.state, "add", "--round", self.round_id, "--severity", "warning",
            "--status", "accept", "message", extra_env=self.fake_env,
        )
        self.assertEqual(payload["error"]["code"], "invalid_status")

    def test_concurrent_open_starts_one_private_tmux_session(self):
        arguments = ("open", "--round", self.round_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: launcher(self.state, *arguments, extra_env=self.fake_env), range(2)))
        for result, payload in results:
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["tmux_session"], "nvim-review")
        log = (self.state / "rounds" / self.round_id / "tmux" / "fake-tmux.log").read_text()
        lines = log.splitlines()
        self.assertEqual(sum(line.startswith("new-session ") for line in lines), 1)
        bootstrap = next(line for line in lines if line.startswith("start-server "))
        self.assertIn("exit-empty off", bootstrap)
        self.assertIn("remain-on-exit failed", bootstrap)
        created = next(index for index, line in enumerate(lines) if line.startswith("new-session "))
        restored = next(index for index, line in enumerate(lines) if line == "set-option -g exit-empty on")
        self.assertLess(created, restored)
        self.assertIn("-f /dev/null", " ".join(results[0][1]["attach_argv"]))

    def test_start_open_combines_capture_and_single_tui_preflight(self):
        result, payload = launcher(
            self.base / "combined-state",
            "start",
            "--repo",
            self.repo,
            "--open",
            extra_env=self.fake_env,
        )
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["command"], "start")
        self.assertEqual(payload["tmux_session"], "nvim-review")
        self.assertTrue(payload["created"])

    def test_tty_attach_drops_parent_tmux_routing(self):
        from tuicr_round import cli

        value = {"id": self.round_id, "repo_root": str(self.repo)}
        attached = {}

        def capture(executable, argv, environment):
            attached.update(executable=executable, argv=argv, environment=environment)
            raise RuntimeError("exec intercepted")

        session = {
            "session": "nvim-review",
            "socket": "/tmp/review.sock",
            "created": False,
            "attach_argv": ["tmux", "-S", "/tmp/review.sock", "attach-session", "-t", "nvim-review"],
        }
        with mock.patch.object(cli, "ensure_session", return_value=session), \
                mock.patch.object(cli, "emit") as emit, \
                mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(cli.sys.stdout, "isatty", return_value=True), \
                mock.patch.object(cli.os, "execvpe", side_effect=capture), \
                mock.patch.dict(cli.os.environ, {"TMUX": "parent,1,0", "TMUX_PANE": "%4", "TERM": "xterm"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "exec intercepted"):
                cli._open_value(self.state, value, pathlib.Path("/launcher"))

        emit.assert_not_called()
        self.assertEqual(attached["executable"], "tmux")
        self.assertNotIn("TMUX", attached["environment"])
        self.assertNotIn("TMUX_PANE", attached["environment"])
        self.assertEqual(attached["environment"]["TERM"], "xterm")

    def test_accept_response_warns_with_copyable_command(self):
        comments = self.base / "comments.json"
        comments.write_text(json.dumps([{
            "id": "root-id",
            "content": '@nvim-review {"version":1,"role":"agent","author":"Agent","severity":"nit","status":"open","reply_to":null}\nroot',
        }]))
        response_env = dict(self.fake_env)
        response_env["FAKE_COMMENTS"] = str(comments)
        result, payload = launcher(
            self.state, "respond", "--round", self.round_id, "--role", "verifier",
            "--author", "Verifier", "--severity", "nit", "--status", "accept",
            "--reply-to", "root-id", "accepted", extra_env=response_env,
        )
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["warnings"][0]["command"], "tuicr-round accepted --round " + self.round_id)

        result, payload = launcher(
            self.state, "respond", "--round", self.round_id, "--role", "agent",
            "--author", "Agent", "--severity", "nit", "--status", "accept",
            "--reply-to", "root-id", "implemented", extra_env=response_env,
        )
        self.assertEqual(result.returncode, 0, payload)
        self.assertNotIn("warnings", payload)

    def test_comments_normalizes_native_protocol_malformed_and_unstructured(self):
        comments_path = self.base / "comments.json"
        response_header = (
            '@nvim-review {"version":1,"role":"agent","author":"Codex GPT-5",'
            '"severity":"blocker","status":"discuss","reply_to":"native"}'
        )
        comments = [
            {
                "id": "native",
                "username": "Reviewer",
                "content": "Please keep the old-side range.",
                "path": "file.txt",
                "start_line": 2,
                "end_line": 4,
                "side": "old",
                "location": "file.txt:2-4",
                "comment_type": "issue",
            },
            {
                "id": "plan",
                "content": response_header + "\nPLAN\nContext and solution",
                "path": "file.txt",
                "start_line": 2,
                "end_line": 4,
                "side": "old",
                "comment_type": "issue",
            },
            {"id": "broken", "content": "@nvim-review not-json"},
            {"id": "", "content": "missing id"},
        ]
        comments_path.write_text(json.dumps(comments))
        environment = dict(self.fake_env)
        environment["FAKE_COMMENTS"] = str(comments_path)

        result, payload = launcher(self.state, "comments", "--repo", self.repo, extra_env=environment)

        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(payload["command"], "comments")
        self.assertEqual(payload["repo_root"], str(self.repo.resolve()))
        snapshot_keys = (
            "branch", "head", "s0_tree", "s0_commit", "b0_tree", "b0_commit", "review_base_commit", "clean",
        )
        self.assertEqual(
            payload["snapshot"],
            {key: self.start_payload[key] for key in snapshot_keys},
        )
        self.assertEqual(payload["handoff"], "TUICR-ROUND:" + self.round_id)
        self.assertEqual(
            payload["prompt"],
            "Use $tuicr-address-review to process TUICR-ROUND:%s." % self.round_id,
        )
        self.assertEqual(payload["comment_digest"], protocol.comment_digest(comments))
        self.assertEqual([item["origin"] for item in payload["comments"]], ["native", "protocol"])
        native, response = payload["comments"]
        self.assertEqual(native["raw"], comments[0])
        self.assertEqual(native["message"], comments[0]["content"])
        self.assertEqual(native["header"]["role"], "human")
        self.assertEqual(native["header"]["author"], "Reviewer")
        self.assertEqual(native["header"]["severity"], "blocker")
        self.assertEqual(native["target"], {
            "path": "file.txt", "start": 2, "end": 4, "side": "old", "location": "file.txt:2-4",
        })
        self.assertEqual(response["message"], "PLAN\nContext and solution")
        self.assertEqual(response["header"]["author"], "Codex GPT-5")
        self.assertEqual(payload["threads"][0]["root_id"], "native")
        self.assertEqual(payload["threads"][0]["latest_id"], "plan")
        self.assertEqual(payload["threads"][0]["latest_role"], "agent")
        self.assertEqual(payload["threads"][0]["latest_author"], "Codex GPT-5")
        self.assertEqual(payload["malformed"], [comments[2]])
        self.assertEqual(payload["unstructured"], [comments[3]])

    def test_respond_inherits_native_old_range_and_allows_explicit_override(self):
        comments_path = self.base / "comments.json"
        comments_path.write_text(json.dumps([{
            "id": "native",
            "content": "Review note",
            "path": "file.txt",
            "start_line": 2,
            "end_line": 4,
            "side": "old",
            "comment_type": "praise",
        }]))
        environment = dict(self.fake_env)
        environment["FAKE_COMMENTS"] = str(comments_path)

        result, payload = launcher(
            self.state, "respond", "--round", self.round_id, "--role", "agent",
            "--author", "Codex GPT-5", "--severity", "nit", "--reply-to", "native",
            "PLAN", extra_env=environment,
        )
        self.assertEqual(result.returncode, 0, payload)
        result, payload = launcher(
            self.state, "respond", "--round", self.round_id, "--role", "agent",
            "--author", "Codex GPT-5", "--severity", "nit", "--reply-to", "native",
            "--path", "file.txt", "--start", "1", "--side", "new", "RESULT", extra_env=environment,
        )
        self.assertEqual(result.returncode, 0, payload)
        additions = [json.loads(line) for line in (self.home / "adds.jsonl").read_text().splitlines()]
        self.assertEqual(
            {key: additions[0].get(key) for key in ("file", "start_line", "end_line", "line", "side")},
            {"file": "file.txt", "start_line": 2, "end_line": 4, "line": None, "side": "old"},
        )
        self.assertEqual(additions[1]["file"], "file.txt")
        self.assertEqual(additions[1]["line"], 1)
        self.assertEqual(additions[1]["side"], "new")
        self.assertNotIn("start_line", additions[1])

    def test_respond_rejects_malformed_target(self):
        comments_path = self.base / "comments.json"
        comments_path.write_text(json.dumps([{"id": "broken", "content": "@nvim-review invalid"}]))
        environment = dict(self.fake_env)
        environment["FAKE_COMMENTS"] = str(comments_path)
        result, payload = launcher(
            self.state, "respond", "--round", self.round_id, "--role", "agent",
            "--author", "Codex GPT-5", "--severity", "warning", "--reply-to", "broken",
            "PLAN", extra_env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "invalid_reply")

    def test_handoff_prompt_and_copy_contract(self):
        expected = "Use $tuicr-address-review to process TUICR-ROUND:%s." % self.round_id
        result, payload = launcher(self.state, "handoff", "--round", self.round_id, extra_env=self.fake_env)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["prompt"], expected)
        self.assertFalse(payload["copied"])

        copied = self.base / "copied.txt"
        write(self.fake_bin / "pbcopy", '#!/bin/sh\ncat > "$PBCOPY_OUT"\n', 0o755)
        environment = dict(self.fake_env)
        environment["PBCOPY_OUT"] = str(copied)
        result, payload = launcher(
            self.state, "handoff", "--round", self.round_id, "--copy", extra_env=environment,
        )
        self.assertEqual(result.returncode, 0, payload)
        self.assertTrue(payload["copied"])
        self.assertEqual(copied.read_text(), expected)

        write(self.fake_bin / "pbcopy", "#!/bin/sh\nexit 17\n", 0o755)
        result, payload = launcher(
            self.state, "handoff", "--round", self.round_id, "--copy", extra_env=self.fake_env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "command_failed")

    def test_start_status_and_native_fallback_metadata(self):
        marker = "TUICR-ROUND:" + self.round_id
        self.assertEqual(self.start_payload["handoff"], marker)
        self.assertEqual(self.start_payload["prompt"], "Use $tuicr-address-review to process %s." % marker)
        result, payload = launcher(self.state, "status", "--round", self.round_id, extra_env=self.fake_env)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["handoff"], marker)
        self.assertEqual(payload["prompt"], "Use $tuicr-address-review to process %s." % marker)

        analysis = protocol.normalize_comments([
            {"id": "praise", "content": "Nice work", "comment_type": "praise", "author": ""},
            {"id": "question", "content": "Why?", "comment_type": "question", "author": "Alice"},
        ])
        self.assertEqual(analysis["comments"][0]["header"]["severity"], "nit")
        self.assertEqual(analysis["comments"][0]["header"]["author"], "Human reviewer")
        self.assertEqual(analysis["comments"][1]["header"]["severity"], "warning")
        self.assertEqual(analysis["comments"][1]["header"]["author"], "Alice")


if __name__ == "__main__":
    unittest.main()
