"""Read-only discovery and external-object Git baselines for review rounds."""

import os
import pathlib
import shutil
import stat
import uuid

from .state import prepare_root, round_dir, round_lock, save_round
from .util import RoundError, atomic_write, ensure_dir, file_lock, isoformat, run


WARN_BYTES = 50 * 1024 * 1024
REJECT_BYTES = 500 * 1024 * 1024
GIT_READ_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
GIT_ROUTING_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_WORK_TREE",
}


def _decode(value, label="Git output"):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise RoundError("unsupported_path_encoding", label + " is not valid UTF-8")


def git_env(extra=None):
    value = os.environ.copy()
    for name in GIT_ROUTING_ENV:
        value.pop(name, None)
    value.update(GIT_READ_ENV)
    if extra:
        value.update({key: str(item) for key, item in extra.items()})
    return value


def git(repo, arguments, check=True, input_bytes=None, extra_env=None):
    return run(
        ["git", "-C", str(repo)] + list(arguments),
        env=git_env(extra_env),
        input_bytes=input_bytes,
        check=check,
    )


def resolve_repo(path):
    completed = git(path, ["rev-parse", "--show-toplevel"], check=False)
    if completed.returncode != 0:
        raise RoundError("not_git_repository", "Path is not inside a Git worktree", {"repo": str(path)})
    return pathlib.Path(_decode(completed.stdout).strip()).resolve()


def _output(repo, arguments):
    return _decode(git(repo, arguments).stdout).strip()


def _config_bool(repo, key):
    result = git(repo, ["config", "--bool", "--get", key], check=False)
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise RoundError("git_config_failed", "Could not inspect Git configuration", {"key": key})
    return _decode(result.stdout).strip().lower() == "true"


def _nul_paths(data):
    paths = []
    for raw in data.split(b"\0"):
        if raw:
            paths.append(_decode(raw, "Git path"))
    return paths


def index_entries(repo):
    result = git(repo, ["ls-files", "--stage", "-z"])
    entries = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_oid, raw_stage = metadata.split(b" ", 2)
        except ValueError:
            raise RoundError("unexpected_git_output", "Could not parse Git index entry")
        entries.append(
            {
                "mode": raw_mode.decode("ascii"),
                "oid": raw_oid.decode("ascii"),
                "stage": int(raw_stage),
                "path": _decode(raw_path, "Git path"),
            }
        )
    return entries


def _reject_unsafe_repo(repo, entries):
    head = git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    if head.returncode != 0:
        raise RoundError("unborn_repository", "Repository HEAD is unborn")
    conflicts = git(repo, ["ls-files", "--unmerged", "-z"]).stdout
    if conflicts:
        raise RoundError("conflicted_repository", "Repository has unresolved index conflicts")

    sparse_reasons = []
    if _config_bool(repo, "core.sparseCheckout"):
        sparse_reasons.append("core.sparseCheckout")
    if _config_bool(repo, "index.sparse"):
        sparse_reasons.append("index.sparse")
    if any(entry["mode"] == "040000" for entry in entries):
        sparse_reasons.append("sparse index entries")
    if sparse_reasons:
        raise RoundError("sparse_checkout", "Sparse checkouts and sparse indexes are unsupported", {"reasons": sparse_reasons})

    shared_index = git(repo, ["rev-parse", "--shared-index-path"], check=False)
    if _config_bool(repo, "core.splitIndex") or (shared_index.returncode == 0 and shared_index.stdout.strip()):
        raise RoundError("split_index", "Split indexes are unsupported")

    partial = git(repo, ["config", "--null", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"], check=False)
    if partial.returncode == 0 and partial.stdout:
        raise RoundError("partial_clone", "Partial and promisor repositories are unsupported")
    common_dir = _output(repo, ["rev-parse", "--git-common-dir"])
    common = pathlib.Path(common_dir)
    if not common.is_absolute():
        common = repo / common
    promisor_packs = list((common.resolve() / "objects" / "pack").glob("*.promisor"))
    if promisor_packs:
        raise RoundError("partial_clone", "Promisor object packs are unsupported")

    all_paths = [entry["path"] for entry in entries if entry["stage"] == 0]
    all_paths.extend(_nul_paths(git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout))
    # check-attr accepts NUL-delimited stdin with -z, preserving unusual names.
    if all_paths:
        payload = b"\0".join(path.encode("utf-8") for path in all_paths) + b"\0"
        attributes = git(repo, ["check-attr", "-z", "--stdin", "filter"], input_bytes=payload).stdout.split(b"\0")
        effective = []
        for offset in range(0, len(attributes) - 2, 3):
            path, attribute, value = attributes[offset : offset + 3]
            if attribute == b"filter" and value not in (b"unspecified", b"unset", b""):
                effective.append(_decode(path, "Git path"))
        if effective:
            raise RoundError(
                "filtered_content",
                "Effective Git filters, including LFS, are unsupported",
                {"paths": sorted(effective)[:20]},
            )
    return head.stdout.decode("ascii").strip(), common.resolve()


def inspect_repo(path):
    repo = resolve_repo(path)
    entries = index_entries(repo)
    head, common = _reject_unsafe_repo(repo, entries)
    return repo, entries, head, common


def _external_env(repo, external_git, real_objects, index_path):
    return git_env(
        {
            "GIT_DIR": external_git,
            "GIT_WORK_TREE": repo,
            "GIT_INDEX_FILE": index_path,
            "GIT_OBJECT_DIRECTORY": pathlib.Path(external_git) / "objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": real_objects,
        }
    )


def _external_git(repo, external_git, real_objects, index_path, arguments, input_bytes=None):
    return run(
        ["git"] + list(arguments),
        cwd=repo,
        env=_external_env(repo, external_git, real_objects, index_path),
        input_bytes=input_bytes,
    )


def _changed_paths(repo):
    staged = _nul_paths(git(repo, ["diff-index", "--cached", "--name-only", "-z", "HEAD", "--",]).stdout)
    tracked = _nul_paths(git(repo, ["diff-files", "--name-only", "-z", "--ignore-submodules=none"]).stdout)
    untracked = _nul_paths(git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout)
    return sorted(set(tracked + untracked)), sorted(set(staged)), sorted(set(tracked)), sorted(set(untracked))


def _staged_bytes(entries, staged_paths, repo):
    by_path = {entry["path"]: entry for entry in entries if entry["stage"] == 0}
    total = 0
    for path in staged_paths:
        entry = by_path.get(path)
        if entry is None or entry["mode"] == "160000":
            continue
        result = git(repo, ["cat-file", "-s", entry["oid"]])
        try:
            total += int(result.stdout.strip())
        except ValueError:
            raise RoundError("unexpected_git_output", "Could not parse staged blob size", {"path": path})
    return total


def _path_size(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(details.st_mode):
        return details.st_size
    if stat.S_ISLNK(details.st_mode):
        return len(os.readlink(str(path)).encode("utf-8"))
    if stat.S_ISDIR(details.st_mode):
        return 0
    raise RoundError("unsupported_worktree_entry", "Only regular files, symlinks, and gitlinks are supported", {"path": str(path)})


def _hash_worktree_path(repo, relative, external_git, real_objects, index_path, indexed_mode=None):
    absolute = repo / relative
    try:
        details = absolute.lstat()
    except FileNotFoundError:
        _external_git(repo, external_git, real_objects, index_path, ["update-index", "--force-remove", "--", relative])
        return "deleted"

    if indexed_mode == "160000" and absolute.is_dir():
        result = git(absolute, ["rev-parse", "--verify", "HEAD"], check=False)
        if result.returncode != 0:
            raise RoundError("invalid_submodule", "Initialized gitlink has no HEAD", {"path": relative})
        mode = "160000"
        oid = result.stdout.decode("ascii").strip()
    elif stat.S_ISLNK(details.st_mode):
        mode = "120000"
        source = os.readlink(str(absolute)).encode("utf-8")
        oid = _external_git(repo, external_git, real_objects, index_path, ["hash-object", "-w", "--stdin"], input_bytes=source).stdout.decode("ascii").strip()
    elif stat.S_ISREG(details.st_mode):
        mode = "100755" if details.st_mode & stat.S_IXUSR else "100644"
        oid = _external_git(
            repo,
            external_git,
            real_objects,
            index_path,
            ["hash-object", "-w", "--no-filters", "--", relative],
        ).stdout.decode("ascii").strip()
    else:
        raise RoundError("unsupported_worktree_entry", "Only regular files, symlinks, and gitlinks are supported", {"path": relative})
    _external_git(repo, external_git, real_objects, index_path, ["update-index", "--add", "--cacheinfo", mode + "," + oid + "," + relative])
    return mode


def _initialize_external(repo, destination, real_objects, entries):
    external_git = destination / "git"
    index_path = destination / "index"
    ensure_dir(destination)
    run(["git", "init", "--bare", str(external_git)], env=git_env())
    for directory in external_git.rglob("*"):
        if directory.is_dir():
            os.chmod(str(directory), 0o700)
    ensure_dir(external_git / "objects" / "info")
    atomic_write(external_git / "objects" / "info" / "alternates", str(real_objects) + "\n")
    _external_git(repo, external_git, real_objects, index_path, ["config", "core.bare", "false"])
    _external_git(repo, external_git, real_objects, index_path, ["config", "core.worktree", str(repo)])
    stage_zero = [entry for entry in entries if entry["stage"] == 0]
    index_info = b"".join(
        (entry["mode"] + " " + entry["oid"] + "\t" + entry["path"]).encode("utf-8") + b"\0"
        for entry in stage_zero
    )
    if index_info:
        _external_git(repo, external_git, real_objects, index_path, ["update-index", "-z", "--index-info"], input_bytes=index_info)
    else:
        _external_git(repo, external_git, real_objects, index_path, ["read-tree", "--empty"])
    return external_git, index_path


def _create_one(root, repo_path, xdg_config_home, parent_round=None):
    repo, entries, head, common = inspect_repo(repo_path)
    changed, staged, unstaged, untracked = _changed_paths(repo)
    worktree_bytes = sum(_path_size(repo / path) for path in changed)
    staged_bytes = _staged_bytes(entries, staged, repo)
    total_bytes = worktree_bytes + staged_bytes
    if total_bytes > REJECT_BYTES:
        raise RoundError(
            "dirty_content_too_large",
            "Dirty worktree content exceeds the 500 MiB safety limit",
            {"bytes": total_bytes, "limit": REJECT_BYTES},
        )
    warnings = []
    if total_bytes > WARN_BYTES:
        warnings.append({"code": "large_dirty_content", "bytes": total_bytes, "threshold": WARN_BYTES})

    round_id = str(uuid.uuid4())
    destination = round_dir(root, round_id)
    ensure_dir(destination)
    ensure_dir(destination / "home")
    ensure_dir(destination / "tmux")
    real_objects = common / "objects"
    external_git, index_path = _initialize_external(repo, destination, real_objects, entries)
    external = lambda args, data=None: _external_git(repo, external_git, real_objects, index_path, args, input_bytes=data)

    s0_tree = external(["write-tree"]).stdout.decode("ascii").strip()
    indexed_modes = {entry["path"]: entry["mode"] for entry in entries if entry["stage"] == 0}
    for relative in changed:
        _hash_worktree_path(repo, relative, external_git, real_objects, index_path, indexed_modes.get(relative))
    b0_tree = external(["write-tree"]).stdout.decode("ascii").strip()
    head_tree = _output(repo, ["rev-parse", head + "^{tree}"])
    branch = "nvim-review-" + round_id.replace("-", "")
    dirty = bool(staged or unstaged or untracked)

    def synthetic_commit(tree, parents, message):
        identity_env = {
            "GIT_AUTHOR_NAME": "nvim-review",
            "GIT_AUTHOR_EMAIL": "nvim-review@localhost",
            "GIT_COMMITTER_NAME": "nvim-review",
            "GIT_COMMITTER_EMAIL": "nvim-review@localhost",
        }
        commit_env = _external_env(repo, external_git, real_objects, index_path)
        commit_env.update(identity_env)
        command = ["git", "commit-tree", tree]
        for parent in parents:
            command.extend(["-p", parent])
        completed = run(
            command,
            cwd=repo,
            env=commit_env,
            input_bytes=(message + "\n").encode("utf-8"),
        )
        return completed.stdout.decode("ascii").strip()

    # Keep staged and unstaged layers as an explicit synthetic commit chain.
    # Comparing only HEAD to B0 loses an MM path when its worktree contents
    # restore HEAD, even though S0 contains a real staged change.
    def transport_base_commit():
        # tuicr resolves an explicit range's aggregate diff before it
        # applies initial_commit_selection=oldest.  When B0 restores HEAD, that
        # aggregate is empty and tuicr exits before S0 can be selected.  A
        # private second-parent base with one transport-only path makes the
        # aggregate nonempty while `base..B0` still enumerates exactly S0 and
        # B0.  Individual commit diffs retain their real first-parent meaning.
        anchor_index = destination / "transport-index"
        shutil.copyfile(str(index_path), str(anchor_index))
        os.chmod(str(anchor_index), 0o600)
        anchor_path = ".nvim-review-transport-" + round_id.replace("-", "")
        anchor_blob = _external_git(
            repo,
            external_git,
            real_objects,
            anchor_index,
            ["hash-object", "-w", "--stdin"],
            input_bytes=(round_id + "\n").encode("ascii"),
        ).stdout.decode("ascii").strip()
        _external_git(
            repo,
            external_git,
            real_objects,
            anchor_index,
            ["update-index", "--add", "--cacheinfo", "100644," + anchor_blob + "," + anchor_path],
        )
        anchor_tree = _external_git(
            repo, external_git, real_objects, anchor_index, ["write-tree"]
        ).stdout.decode("ascii").strip()
        anchor_index.unlink()
        return synthetic_commit(anchor_tree, [head], "nvim-review transport base")

    s0_commit = head
    if s0_tree != head_tree:
        s0_commit = synthetic_commit(s0_tree, [head], "nvim-review staged snapshot S0")
    review_base_commit = head
    b0_commit = s0_commit
    if b0_tree != s0_tree:
        if b0_tree == head_tree:
            review_base_commit = transport_base_commit()
            b0_commit = synthetic_commit(
                b0_tree,
                [s0_commit, review_base_commit],
                "nvim-review worktree snapshot B0",
            )
        else:
            b0_commit = synthetic_commit(b0_tree, [s0_commit], "nvim-review worktree snapshot B0")
    elif dirty and b0_commit == head:
        # A dirty gitlink may have no parent-tree delta because its child HEAD
        # is unchanged. Keep the parent round nonempty; the child round carries
        # the actual submodule worktree snapshot.
        review_base_commit = transport_base_commit()
        b0_commit = synthetic_commit(
            b0_tree,
            [head, review_base_commit],
            "nvim-review dirty snapshot B0",
        )
    clean = not dirty
    if clean:
        parents = _output(repo, ["rev-list", "--parents", "-n", "1", head]).split()
        if len(parents) > 1:
            review_base_commit = parents[1]
        else:
            empty_tree = external(["mktree"], b"").stdout.decode("ascii").strip()
            review_base_commit = synthetic_commit(
                empty_tree,
                [],
                "nvim-review empty root base",
            )
    external(["update-ref", "refs/heads/" + branch, b0_commit])
    atomic_write(external_git / "HEAD", "ref: refs/heads/" + branch + "\n")

    value = {
        "version": 1,
        "id": round_id,
        "repo_root": str(repo),
        "created_at": isoformat(),
        "closed_at": None,
        "owner_pid": None,
        "parent_round": parent_round,
        "submodule_rounds": [],
        "branch": branch,
        "head": head,
        "head_tree": head_tree,
        "s0_tree": s0_tree,
        "s0_commit": s0_commit,
        "b0_tree": b0_tree,
        "b0_commit": b0_commit,
        "review_base_commit": review_base_commit,
        "clean": clean,
        "dirty_paths": sorted(set(staged + unstaged + untracked)),
        "staged_paths": staged,
        "unstaged_paths": unstaged,
        "untracked_paths": untracked,
        "dirty_bytes": total_bytes,
        "staged_bytes": staged_bytes,
        "worktree_bytes": worktree_bytes,
        "warnings": warnings,
        "external_git_dir": str(external_git),
        "external_index": str(index_path),
        "real_objects": str(real_objects),
        "private_home": str(destination / "home"),
        "xdg_config_home": str(pathlib.Path(xdg_config_home).resolve()),
        "session": None,
    }
    save_round(root, value)
    for item in destination.rglob("*"):
        if item.is_dir():
            os.chmod(str(item), 0o700)
        elif item.is_file():
            os.chmod(str(item), 0o600)
    return value, entries


def _dirty_initialized_submodules(repo, entries):
    roots = []
    for entry in entries:
        if entry["stage"] != 0 or entry["mode"] != "160000":
            continue
        candidate = repo / entry["path"]
        if not candidate.is_dir():
            continue
        top = git(candidate, ["rev-parse", "--show-toplevel"], check=False)
        if top.returncode != 0:
            continue
        status = git(candidate, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"])
        if status.stdout:
            roots.append(resolve_repo(candidate))
    return roots


def start_rounds(repo_path, root, xdg_config_home):
    root = prepare_root(root)
    rounds = root / "rounds"
    with file_lock(root / "start.lock"):
        before = {path.name for path in rounds.iterdir()}
        created = []
        visited = set()

        def visit(path, parent=None):
            canonical = resolve_repo(path)
            if str(canonical) in visited:
                return None
            visited.add(str(canonical))
            value, entries = _create_one(root, canonical, xdg_config_home, parent_round=parent)
            created.append(value)
            child_ids = []
            for submodule in _dirty_initialized_submodules(canonical, entries):
                child = visit(submodule, parent=value["id"])
                if child is not None:
                    child_ids.append(child["id"])
            if child_ids:
                with round_lock(root, value["id"]):
                    value["submodule_rounds"] = child_ids
                    save_round(root, value)
            return value

        try:
            parent = visit(repo_path)
        except Exception:
            # The start lock makes the set difference belong to this one
            # capture. Remove both completed parents and any partial child
            # directory so a failed multi-repo start cannot become selectable.
            for path in sorted(rounds.iterdir(), key=lambda item: item.name, reverse=True):
                if path.name in before:
                    continue
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(str(path))
            raise
        return parent, created


def external_environment(round_value):
    return _external_env(
        pathlib.Path(round_value["repo_root"]),
        pathlib.Path(round_value["external_git_dir"]),
        pathlib.Path(round_value["real_objects"]),
        pathlib.Path(round_value["external_index"]),
    )
