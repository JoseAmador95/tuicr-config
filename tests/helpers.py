import hashlib
import json
import os
import pathlib
import subprocess


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "tuicr-round"


def command(argv, cwd=None, env=None, input_bytes=None, check=True):
    merged = os.environ.copy()
    if env:
        merged.update({key: str(value) for key, value in env.items()})
    result = subprocess.run(
        [str(value) for value in argv],
        cwd=str(cwd) if cwd else None,
        env=merged,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError("command failed: %r\n%s" % (argv, result.stderr.decode("utf-8", "replace")))
    return result


def git(repo, *arguments, check=True, env=None, input_bytes=None):
    return command(["git", "-C", repo] + list(arguments), env=env, input_bytes=input_bytes, check=check)


def init_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Fixture User")
    git(path, "config", "user.email", "fixture@example.invalid")
    return path


def write(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)


def commit_all(repo, message="fixture"):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def launcher(state, *arguments, cwd=None, extra_env=None):
    env = {"NVIM_REVIEW_STATE_HOME": state, "PYTHONPYCACHEPREFIX": "/tmp/tuicr-round-tests-pycache"}
    if extra_env:
        env.update(extra_env)
    result = command([LAUNCHER] + list(arguments), cwd=cwd, env=env, check=False)
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        raise AssertionError("launcher did not return JSON: %r %r" % (result.stdout, result.stderr))
    return result, payload


def fingerprint(repo):
    git_dir = pathlib.Path(git(repo, "rev-parse", "--git-dir").stdout.decode().strip())
    if not git_dir.is_absolute():
        git_dir = pathlib.Path(repo) / git_dir
    files = {}
    for path in sorted(git_dir.rglob("*")):
        if not path.is_file() or "logs" in path.parts:
            continue
        relative = str(path.relative_to(git_dir))
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "head": git(repo, "rev-parse", "HEAD", check=False).stdout,
        "status": git(repo, "status", "--porcelain=v1", "-z").stdout,
        "git_files": files,
    }


def external_env(round_value):
    return {
        "GIT_DIR": round_value["external_git_dir"],
        "GIT_WORK_TREE": round_value["repo_root"],
        "GIT_INDEX_FILE": round_value["external_index"],
        "GIT_OBJECT_DIRECTORY": str(pathlib.Path(round_value["external_git_dir"]) / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": round_value["real_objects"],
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
