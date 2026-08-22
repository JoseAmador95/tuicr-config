import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tuicr_round import protocol, tmux_control
from tuicr_round.handoff import handoff_prompt


class TuiLaunchTests(unittest.TestCase):
    def _launch(self, clean):
        value = {
            "clean": clean,
            "head": "a" * 40,
            "b0_commit": "b" * 40,
            "review_base_commit": "c" * 40,
            "repo_root": tempfile.gettempdir(),
        }
        captured = {}

        def execute(executable, argv, environment):
            captured["executable"] = executable
            captured["argv"] = argv
            captured["environment"] = environment
            raise RuntimeError("captured")

        with mock.patch.object(tmux_control, "check_tuicr_available"), mock.patch.object(
            tmux_control, "tuicr_environment", return_value=os.environ.copy()
        ), mock.patch.object(tmux_control, "emit"), mock.patch.object(
            tmux_control.os, "chdir"
        ), mock.patch.object(
            tmux_control.os, "execvpe", side_effect=execute
        ):
            self.assertEqual(tmux_control.launch_tuicr(value), 1)
        return captured["argv"]

    def test_clean_round_reviews_frozen_commit_range(self):
        argv = self._launch(True)
        self.assertEqual(argv[:3], ["tuicr", "-r", "c" * 40 + ".." + "b" * 40])
        self.assertNotIn("-w", argv)

    def test_dirty_round_reviews_synthetic_baseline_and_worktree(self):
        argv = self._launch(False)
        self.assertEqual(argv[:3], ["tuicr", "-r", "c" * 40 + ".." + "b" * 40])
        self.assertNotIn("-w", argv)


class PbcopyWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.private_home = self.base / "private-home"
        self.real_bin = self.base / "real-bin"
        self.real_bin.mkdir()
        self.output = self.base / "clipboard.bin"
        self.round_id = "12345678-1234-5678-9234-567812345678"
        self.round_value = {
            "id": self.round_id,
            "private_home": str(self.private_home),
            "xdg_config_home": str(self.base / "original-xdg"),
        }
        real_pbcopy = self.real_bin / "pbcopy"
        real_pbcopy.write_text(
            """#!%s
import os
import pathlib
import sys

pathlib.Path(os.environ["PBCOPY_OUTPUT"]).write_bytes(sys.stdin.buffer.read())
raise SystemExit(int(os.environ.get("PBCOPY_EXIT", "0")))
""" % sys.executable,
            encoding="utf-8",
        )
        real_pbcopy.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def _prepare_wrapper(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.real_bin)}):
            return protocol.prepare_pbcopy_wrapper(self.round_value)

    def _copy(self, wrapper, payload, exit_code=0):
        environment = os.environ.copy()
        environment["PBCOPY_OUTPUT"] = str(self.output)
        environment["PBCOPY_EXIT"] = str(exit_code)
        return subprocess.run(
            [str(wrapper)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

    def test_wrapper_prepends_once_and_preserves_urls_and_bytes(self):
        wrapper = self._prepare_wrapper()
        prompt = handoff_prompt(self.round_id).encode("utf-8")
        cases = (
            (b"review\n", prompt + b"\n\nreview\n"),
            (b"header\n" + prompt + b"\nbody\n", b"header\n" + prompt + b"\nbody\n"),
            (b"http://example.test/pull/1\n", b"http://example.test/pull/1\n"),
            (b"https://example.test/pull/2", b"https://example.test/pull/2"),
            (b"\x00\xffreview\n\n", prompt + b"\n\n\x00\xffreview\n\n"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                result = self._copy(wrapper, payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.output.read_bytes(), expected)

    def test_wrapper_forwards_target_failure(self):
        wrapper = self._prepare_wrapper()
        result = self._copy(wrapper, b"review", exit_code=23)
        self.assertEqual(result.returncode, 23)

    def test_environment_changes_path_only_after_wrapper_exists(self):
        original = {"PATH": "/original/bin", "XDG_CONFIG_HOME": "/current-xdg", "OTHER": "preserved"}
        with mock.patch.object(protocol, "external_environment", side_effect=lambda unused: dict(original)):
            before = protocol.tuicr_environment(self.round_value)
            wrapper = self._prepare_wrapper()
            after = protocol.tuicr_environment(self.round_value)
        self.assertEqual(before["PATH"], original["PATH"])
        self.assertEqual(after["PATH"], str(wrapper.parent) + os.pathsep + original["PATH"])
        self.assertEqual(before["XDG_CONFIG_HOME"], self.round_value["xdg_config_home"])
        self.assertEqual(after["XDG_CONFIG_HOME"], self.round_value["xdg_config_home"])
        self.assertEqual(before["OTHER"], "preserved")
        self.assertEqual(after["OTHER"], "preserved")


if __name__ == "__main__":
    unittest.main()
