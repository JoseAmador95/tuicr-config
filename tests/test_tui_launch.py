import os
import tempfile
import unittest
from unittest import mock

from tuicr_round import tmux_control


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

        with mock.patch.object(tmux_control, "check_version"), mock.patch.object(
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


if __name__ == "__main__":
    unittest.main()
