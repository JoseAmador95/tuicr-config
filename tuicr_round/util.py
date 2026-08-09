"""Small, Python 3.9-compatible safety primitives used by tuicr-round."""

import contextlib
import datetime as _datetime
import errno
import fcntl
import json
import os
import pathlib
import subprocess
import tempfile


class RoundError(Exception):
    """An expected error which is safe to return as structured JSON."""

    def __init__(self, code, message, details=None, exit_code=2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code


def utc_now():
    return _datetime.datetime.now(_datetime.timezone.utc)


def isoformat(value=None):
    value = value or utc_now()
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = _datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.astimezone(_datetime.timezone.utc)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def emit(value, stream=None):
    import sys

    target = stream or sys.stdout
    target.write(json_bytes(value).decode("utf-8"))
    target.flush()


def ensure_dir(path):
    path = pathlib.Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)
    return path


def atomic_write(path, data, mode=0o600):
    path = pathlib.Path(path)
    ensure_dir(path.parent)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_json(path, value):
    atomic_write(path, json_bytes(value))


def read_json(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        raise RoundError("invalid_state", "Round state cannot be read", {"path": str(path), "reason": str(error)})


@contextlib.contextmanager
def file_lock(path):
    path = pathlib.Path(path)
    ensure_dir(path.parent)
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run(argv, cwd=None, env=None, input_bytes=None, check=True):
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        raise RoundError("missing_dependency", "Required executable was not found", {"executable": str(argv[0])})
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise RoundError(
            "command_failed",
            "A required command failed",
            {"argv": [str(part) for part in argv], "exit_code": completed.returncode, "stderr": stderr},
        )
    return completed


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True
