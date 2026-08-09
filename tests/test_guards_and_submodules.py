import json
import os
import pathlib
import tempfile
import unittest

from helpers import commit_all, fingerprint, git, init_repo, launcher, write


class RepositoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def repo(self):
        self.counter += 1
        repo = init_repo(self.base / ("repo-%d" % self.counter))
        write(repo / "file.txt", "base\n")
        commit_all(repo)
        return repo

    def assert_rejected(self, repo, code):
        before = fingerprint(repo)
        result, payload = launcher(self.base / ("state-%d" % self.counter), "start", "--repo", repo)
        self.assertNotEqual(result.returncode, 0, payload)
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(before, fingerprint(repo))

    def test_unborn_conflicts_sparse_split_partial_and_filters_fail_closed(self):
        unborn = init_repo(self.base / "unborn")
        self.counter += 1
        self.assert_rejected(unborn, "unborn_repository")

        conflicted = self.repo()
        base_branch = git(conflicted, "branch", "--show-current").stdout.decode().strip()
        git(conflicted, "checkout", "-qb", "other")
        write(conflicted / "file.txt", "other\n")
        commit_all(conflicted, "other")
        git(conflicted, "checkout", "-q", base_branch)
        write(conflicted / "file.txt", "master\n")
        commit_all(conflicted, "master")
        git(conflicted, "merge", "other", check=False)
        self.assert_rejected(conflicted, "conflicted_repository")

        sparse = self.repo()
        git(sparse, "config", "core.sparseCheckout", "true")
        self.assert_rejected(sparse, "sparse_checkout")

        split = self.repo()
        git(split, "update-index", "--split-index")
        self.assert_rejected(split, "split_index")

        partial = self.repo()
        git(partial, "config", "extensions.partialClone", "origin")
        self.assert_rejected(partial, "partial_clone")

        filtered = self.repo()
        write(filtered / ".gitattributes", "*.dat filter=custom\n")
        write(filtered / "payload.dat", "payload\n")
        self.assert_rejected(filtered, "filtered_content")

        lfs = self.repo()
        write(lfs / ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        write(lfs / "payload.bin", b"payload")
        self.assert_rejected(lfs, "filtered_content")

    def test_dirty_initialized_submodule_gets_separate_round(self):
        child = init_repo(self.base / "child")
        write(child / "child.txt", "base\n")
        commit_all(child)
        parent = init_repo(self.base / "parent")
        write(parent / "parent.txt", "base\n")
        commit_all(parent)
        git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
        commit_all(parent, "submodule")
        write(parent / "sub" / "child.txt", "dirty\n")
        before = fingerprint(parent)

        result, payload = launcher(self.base / "state-sub", "start", "--repo", parent)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(len(payload["rounds"]), 2)
        roots = {item["repo_root"] for item in payload["rounds"]}
        self.assertEqual(roots, {str(parent.resolve()), str((parent / "sub").resolve())})
        child_round = [item for item in payload["rounds"] if item["repo_root"] == str((parent / "sub").resolve())][0]
        self.assertEqual(child_round["parent_round"], payload["round"])
        self.assertEqual(before, fingerprint(parent))

    def test_rejected_dirty_submodule_rolls_back_parent_round(self):
        child = init_repo(self.base / "child-rejected")
        write(child / "child.txt", "base\n")
        commit_all(child)
        parent = init_repo(self.base / "parent-rejected")
        write(parent / "parent.txt", "base\n")
        commit_all(parent)
        git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
        commit_all(parent, "submodule")
        write(parent / "sub" / ".gitattributes", "*.dat filter=custom\n")
        write(parent / "sub" / "payload.dat", "dirty\n")
        before = fingerprint(parent)
        state = self.base / "state-rejected-sub"

        result, payload = launcher(state, "start", "--repo", parent)
        self.assertNotEqual(result.returncode, 0, payload)
        self.assertEqual(payload["error"]["code"], "filtered_content")
        self.assertEqual(before, fingerprint(parent))
        self.assertEqual(list((state / "rounds").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
