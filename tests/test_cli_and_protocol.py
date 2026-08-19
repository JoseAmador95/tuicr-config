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


if __name__ == "__main__":
    unittest.main()
