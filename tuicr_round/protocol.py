"""tuicr 0.21 public-CLI integration and nvim-review comment protocol."""

import hashlib
import json
import os
import pathlib

from .git_baseline import external_environment, git
from .state import round_lock, update_round
from .util import RoundError, json_bytes, run


TUICR_VERSION = "0.21.0"
HEADER_PREFIX = "@nvim-review "
PROTOCOL_VERSION = 1
ROLES = ("human", "agent", "verifier")
SEVERITIES = ("blocker", "warning", "nit")
STATUSES = ("open", "accept", "discuss", "reject")
TYPE_BY_SEVERITY = {"blocker": "issue", "warning": "suggestion", "nit": "pedantic"}


def check_version():
    result = run(["tuicr", "--version"])
    actual = result.stdout.decode("utf-8", "replace").strip()
    expected = "tuicr " + TUICR_VERSION
    if actual != expected:
        raise RoundError("unsupported_tuicr_version", "tuicr 0.21.0 is required", {"expected": expected, "actual": actual})


def tuicr_environment(round_value):
    value = external_environment(round_value)
    value["HOME"] = round_value["private_home"]
    value["XDG_CONFIG_HOME"] = round_value["xdg_config_home"]
    value["GIT_OPTIONAL_LOCKS"] = "0"
    value["GIT_NO_LAZY_FETCH"] = "1"
    return value


def _tuicr_json(round_value, arguments, input_value=None):
    encoded = json_bytes(input_value) if input_value is not None else None
    completed = run(
        ["tuicr"] + list(arguments),
        cwd=round_value["repo_root"],
        env=tuicr_environment(round_value),
        input_bytes=encoded,
    )
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RoundError(
            "invalid_tuicr_json",
            "tuicr returned invalid JSON",
            {"reason": str(error), "stdout": completed.stdout.decode("utf-8", "replace")[:1000]},
        )


def resolve_session(root, round_value):
    sessions = _tuicr_json(round_value, ["review", "list", "--repo", round_value["repo_root"]])
    if not isinstance(sessions, list):
        raise RoundError("invalid_tuicr_json", "tuicr review list did not return an array")
    if len(sessions) > 1:
        raise RoundError(
            "ambiguous_tuicr_session",
            "The private round HOME contains more than one matching tuicr session",
            {"slugs": sorted(str(item.get("slug")) for item in sessions if isinstance(item, dict))},
        )
    if not sessions:
        if round_value.get("session") is not None:
            with round_lock(root, round_value["id"], "protocol"):
                round_value["session"] = None
                update_round(root, round_value["id"], {"session": None})
        return None
    session = sessions[0]
    if not isinstance(session, dict) or not isinstance(session.get("slug"), str) or not session["slug"]:
        raise RoundError("invalid_tuicr_json", "tuicr review list returned a session without a slug")
    path = session.get("path")
    if not isinstance(path, str) or not path:
        raise RoundError("invalid_tuicr_json", "tuicr review list returned a session without a path")
    private_home = pathlib.Path(round_value["private_home"]).resolve()
    try:
        pathlib.Path(path).resolve().relative_to(private_home)
    except ValueError:
        raise RoundError("session_isolation_failed", "tuicr session escaped the round-private HOME", {"path": path})
    persisted = {"slug": session["slug"], "path": path}
    if round_value.get("session") != persisted:
        with round_lock(root, round_value["id"], "protocol"):
            round_value["session"] = persisted
            update_round(root, round_value["id"], {"session": persisted})
    return session


def require_session(root, round_value):
    session = resolve_session(root, round_value)
    if session is None:
        raise RoundError("session_not_ready", "No tuicr session exists yet; open the round first")
    return session


def get_comments(root, round_value):
    session = require_session(root, round_value)
    return comments_for_session(round_value, session)


def comments_for_session(round_value, session):
    comments = _tuicr_json(
        round_value,
        ["review", "comments", "--session", session["slug"], "--repo", round_value["repo_root"]],
    )
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        raise RoundError("invalid_tuicr_json", "tuicr review comments did not return an array of objects")
    return comments


def normalize_target(repo_root, path, start=None, end=None):
    if path is None:
        if start is not None or end is not None:
            raise RoundError("invalid_target", "Line positions require --path")
        return None, None, None
    if not isinstance(path, str) or not path or "\\" in path:
        raise RoundError("invalid_target", "Target path must be a non-empty repo-relative POSIX path")
    pure = pathlib.PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise RoundError("invalid_target", "Target path must stay within the repository", {"path": path})
    normalized = str(pure)
    if start is not None and (not isinstance(start, int) or isinstance(start, bool) or start < 1):
        raise RoundError("invalid_target", "Start line must be a positive integer")
    if end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < 1):
        raise RoundError("invalid_target", "End line must be a positive integer")
    if end is not None and start is None:
        raise RoundError("invalid_target", "End line requires a start line")
    if start is not None and end is not None and end < start:
        raise RoundError("invalid_target", "End line must not precede start line")
    return normalized, start, end


def resolve_author(round_value, role, author):
    if role not in ROLES:
        raise RoundError("invalid_role", "Unsupported protocol role", {"role": role})
    if role in ("agent", "verifier"):
        if not isinstance(author, str) or not author.strip():
            raise RoundError("author_required", "Agent and verifier comments require an explicit --author")
        return author.strip()
    if author is not None:
        if not author.strip():
            raise RoundError("invalid_author", "Author must not be empty")
        return author.strip()
    result = git(round_value["repo_root"], ["config", "--get", "user.name"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RoundError("missing_human_author", "Set git user.name or pass --author for a human comment")
    return result.stdout.decode("utf-8", "replace").strip()


def header(role, author, severity, status, reply_to):
    if severity not in SEVERITIES:
        raise RoundError("invalid_severity", "Unsupported severity", {"severity": severity})
    if status not in STATUSES:
        raise RoundError("invalid_status", "Unsupported protocol status", {"status": status})
    if reply_to is not None and (not isinstance(reply_to, str) or not reply_to.strip()):
        raise RoundError("invalid_reply", "reply_to must be a non-empty comment id or null")
    value = {
        "version": PROTOCOL_VERSION,
        "role": role,
        "author": author,
        "severity": severity,
        "status": status,
        "reply_to": reply_to,
    }
    return HEADER_PREFIX + json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def add_comment(root, round_value, role, author, severity, status, message, reply_to=None, path=None, start=None, end=None):
    if not isinstance(message, str) or not message.strip():
        raise RoundError("invalid_message", "Comment message must not be empty")
    author = resolve_author(round_value, role, author)
    path, start, end = normalize_target(round_value["repo_root"], path, start, end)
    if reply_to is None and status != "open":
        raise RoundError("invalid_status", "Root comments must start with status open")
    if reply_to is not None:
        comments = get_comments(root, round_value)
        target = next((item for item in comments if item.get("id") == reply_to), None)
        if target is None or parse_header(target) is None:
            raise RoundError(
                "invalid_reply",
                "reply_to must identify a structured comment in this round",
                {"reply_to": reply_to},
            )
    content = header(role, author, severity, status, reply_to) + "\n" + message
    payload = {"type": TYPE_BY_SEVERITY[severity], "content": content, "side": "new"}
    if path is not None:
        payload["file"] = path
        if start is not None and end is not None and start != end:
            payload["start_line"] = start
            payload["end_line"] = end
        elif start is not None:
            payload["line"] = start
    session = require_session(root, round_value)
    with round_lock(root, round_value["id"], "protocol"):
        result = _tuicr_json(
            round_value,
            [
                "review",
                "add",
                "--session",
                session["slug"],
                "--repo",
                round_value["repo_root"],
                "--input",
                "-",
                "--username",
                author,
            ],
            input_value=payload,
        )
    return result, author, payload


def parse_header(comment):
    content = comment.get("content")
    if not isinstance(content, str):
        return None
    first = content.splitlines()[0] if content.splitlines() else ""
    if not first.startswith(HEADER_PREFIX):
        return None
    try:
        value = json.loads(first[len(HEADER_PREFIX) :])
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    required = {"version", "role", "author", "severity", "status", "reply_to"}
    if set(value) != required:
        return None
    if value["version"] != PROTOCOL_VERSION or value["role"] not in ROLES or value["severity"] not in SEVERITIES or value["status"] not in STATUSES:
        return None
    if not isinstance(value["author"], str) or not value["author"]:
        return None
    if value["reply_to"] is not None and (not isinstance(value["reply_to"], str) or not value["reply_to"]):
        return None
    return value


def analyze_threads(comments):
    parsed = []
    unstructured = []
    by_id = {}
    for position, comment in enumerate(comments):
        comment_id = comment.get("id")
        metadata = parse_header(comment)
        if not isinstance(comment_id, str) or not comment_id or metadata is None:
            unstructured.append(comment)
            continue
        record = {"id": comment_id, "comment": comment, "header": metadata, "position": position}
        parsed.append(record)
        by_id[comment_id] = record

    def root_id(record):
        seen = set()
        current = record
        while current["header"]["reply_to"] is not None:
            target = current["header"]["reply_to"]
            if target in seen or target not in by_id:
                return None
            seen.add(target)
            current = by_id[target]
        return current["id"]

    groups = {}
    malformed = []
    for record in parsed:
        root = root_id(record)
        if root is None:
            malformed.append(record["comment"])
            continue
        groups.setdefault(root, []).append(record)
    threads = []
    for root, records in groups.items():
        records.sort(key=lambda item: item["position"])
        latest = records[-1]
        threads.append(
            {
                "root_id": root,
                "status": latest["header"]["status"],
                "severity": by_id[root]["header"]["severity"],
                "comment": by_id[root]["comment"],
                "latest_id": latest["id"],
                "comment_ids": [item["id"] for item in records],
            }
        )
    threads.sort(key=lambda item: item["root_id"])
    return {"threads": threads, "unstructured": unstructured, "malformed": malformed}


def comment_digest(comments):
    canonical = json.dumps(comments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def confirmation_token(round_id, digest):
    return hashlib.sha256(("nvim-review-close\0" + round_id + "\0" + digest).encode("ascii")).hexdigest()[:32]
