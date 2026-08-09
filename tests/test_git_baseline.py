import json
import os
import pathlib
import stat
import tempfile
import unittest

from helpers import commit_all, external_env, fingerprint, git, init_repo, launcher, write


class SyntheticBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.repo = init_repo(self.base / "repo")
        self.state = self.base / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def _load_round(self, round_id):
        return json.loads((self.state / "rounds" / round_id / "round.json").read_text(encoding="utf-8"))

    def test_mixed_dirty_content_and_real_repo_invariants(self):
        write(self.repo / ".gitignore", "ignored.bin\n")
        write(self.repo / "plain.txt", "plain old\n")
        write(self.repo / "executable.sh", "#!/bin/sh\nexit 0\n", 0o755)
        write(self.repo / "old-name.txt", "rename me\n")
        write(self.repo / "staged.txt", "base\n")
        commit_all(self.repo)

        write(self.repo / "plain.txt", "plain new\n")
        os.rename(str(self.repo / "old-name.txt"), str(self.repo / "new-name.txt"))
        write(self.repo / "staged.txt", "staged version\n")
        git(self.repo, "add", "staged.txt")
        write(self.repo / "staged.txt", "worktree version\n")
        write(self.repo / "binary.bin", b"\x00\xff\x10binary\n")
        os.symlink("plain.txt", str(self.repo / "link"))
        write(self.repo / "ignored.bin", b"ignored")
        before = fingerprint(self.repo)

        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(before, fingerprint(self.repo))
        value = self._load_round(payload["round"])
        self.assertEqual(value["staged_paths"], ["staged.txt"])
        self.assertIn("plain.txt", value["unstaged_paths"])
        self.assertIn("binary.bin", value["untracked_paths"])
        self.assertNotIn("ignored.bin", value["dirty_paths"])

        env = external_env(value)
        def show(path):
            return git(self.repo, "show", value["b0_commit"] + ":" + path, env=env).stdout

        self.assertEqual(show("plain.txt"), b"plain new\n")
        self.assertEqual(show("staged.txt"), b"worktree version\n")
        self.assertEqual(show("new-name.txt"), b"rename me\n")
        self.assertEqual(show("binary.bin"), b"\x00\xff\x10binary\n")
        self.assertEqual(show("link"), b"plain.txt")
        self.assertNotEqual(git(self.repo, "cat-file", "-e", value["b0_commit"] + ":old-name.txt", env=env, check=False).returncode, 0)
        self.assertNotEqual(git(self.repo, "cat-file", "-e", value["b0_commit"] + ":ignored.bin", env=env, check=False).returncode, 0)
        stage_mode = git(self.repo, "ls-tree", value["b0_tree"], "executable.sh", env=env).stdout.split()[0]
        link_mode = git(self.repo, "ls-tree", value["b0_tree"], "link", env=env).stdout.split()[0]
        self.assertEqual(stage_mode, b"100755")
        self.assertEqual(link_mode, b"120000")
        self.assertEqual(git(self.repo, "show", value["s0_tree"] + ":staged.txt", env=env).stdout, b"staged version\n")

        root = self.state.resolve()
        for path in root.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))

    def test_clean_repo_reuses_head(self):
        write(self.repo / "base", "base\n")
        commit_all(self.repo)
        base_commit = git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        write(self.repo / "clean", "yes\n")
        commit_all(self.repo)
        before = fingerprint(self.repo)
        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(before, fingerprint(self.repo))
        value = self._load_round(payload["round"])
        self.assertTrue(value["clean"])
        self.assertEqual(value["head"], value["b0_commit"])
        self.assertEqual(value["review_base_commit"], base_commit)
        self.assertEqual(value["head_tree"], value["s0_tree"])
        self.assertEqual(value["head_tree"], value["b0_tree"])

    def test_clean_root_commit_uses_external_empty_base(self):
        write(self.repo / "root", "root\n")
        commit_all(self.repo)
        before = fingerprint(self.repo)

        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(before, fingerprint(self.repo))
        value = self._load_round(payload["round"])
        self.assertTrue(value["clean"])
        self.assertNotEqual(value["review_base_commit"], value["head"])
        env = external_env(value)
        commits = git(
            self.repo,
            "rev-list",
            "--reverse",
            value["review_base_commit"] + ".." + value["b0_commit"],
            env=env,
        ).stdout.splitlines()
        self.assertEqual(commits, [value["head"].encode("ascii")])

    def test_staged_and_unstaged_cancellation_remains_reviewable(self):
        write(self.repo / "layered.txt", "base\n")
        commit_all(self.repo)
        write(self.repo / "layered.txt", "staged\n")
        git(self.repo, "add", "layered.txt")
        write(self.repo / "layered.txt", "base\n")
        before = fingerprint(self.repo)

        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(before, fingerprint(self.repo))
        value = self._load_round(payload["round"])
        self.assertFalse(value["clean"])
        self.assertEqual(payload["s0_commit"], value["s0_commit"])
        self.assertEqual(value["staged_paths"], ["layered.txt"])
        self.assertEqual(value["unstaged_paths"], ["layered.txt"])
        self.assertNotEqual(value["s0_commit"], value["head"])
        self.assertNotEqual(value["b0_commit"], value["head"])
        self.assertEqual(value["b0_tree"], value["head_tree"])

        env = external_env(value)
        commits = git(
            self.repo,
            "rev-list",
            "--reverse",
            value["review_base_commit"] + ".." + value["b0_commit"],
            env=env,
        ).stdout.splitlines()
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits, [value["s0_commit"].encode("ascii"), value["b0_commit"].encode("ascii")])
        parents = git(self.repo, "rev-list", "--parents", "-1", value["b0_commit"], env=env).stdout.split()
        self.assertEqual(parents[1:], [value["s0_commit"].encode("ascii"), value["review_base_commit"].encode("ascii")])
        self.assertEqual(git(self.repo, "diff", value["head"] + ".." + value["b0_commit"], env=env).stdout, b"")
        self.assertNotEqual(
            git(self.repo, "diff", value["review_base_commit"] + ".." + value["b0_commit"], env=env).stdout,
            b"",
        )
        self.assertEqual(git(self.repo, "show", value["s0_commit"] + ":layered.txt", env=env).stdout, b"staged\n")
        self.assertEqual(git(self.repo, "show", value["b0_commit"] + ":layered.txt", env=env).stdout, b"base\n")

    def test_inherited_git_routing_environment_is_ignored(self):
        write(self.repo / "wanted", "wanted\n")
        commit_all(self.repo)
        other = init_repo(self.base / "other")
        write(other / "other", "other\n")
        commit_all(other)
        result, payload = launcher(
            self.state,
            "start",
            "--repo",
            self.repo,
            extra_env={"GIT_DIR": other / ".git", "GIT_WORK_TREE": other},
        )
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["repo_root"], str(self.repo.resolve()))

    def test_exact_size_thresholds_use_sparse_files(self):
        write(self.repo / "base", "x\n")
        commit_all(self.repo)
        warning_file = self.repo / "warning.bin"
        with warning_file.open("wb") as handle:
            handle.truncate(50 * 1024 * 1024 + 1)
        result, payload = launcher(self.state, "start", "--repo", self.repo)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["warnings"][0]["code"], "large_dirty_content")

        reject_repo = init_repo(self.base / "reject")
        write(reject_repo / "base", "x\n")
        commit_all(reject_repo)
        with (reject_repo / "reject.bin").open("wb") as handle:
            handle.truncate(500 * 1024 * 1024 + 1)
        result, payload = launcher(self.base / "reject-state", "start", "--repo", reject_repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"]["code"], "dirty_content_too_large")
        rounds = self.base / "reject-state" / "rounds"
        self.assertEqual(list(rounds.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
