"""tuicr public-CLI integration and nvim-review comment protocol."""

import hashlib
import json
import os
import pathlib

from .git_baseline import external_environment, git
from .state import round_lock, update_round
from .util import RoundError, json_bytes, run


HEADER_PREFIX = "@nvim-review "
PROTOCOL_VERSION = 1
ROLES = ("human", "agent", "verifier")
SEVERITIES = ("blocker", "warning", "nit")
STATUSES = ("open", "accept", "discuss", "reject")
TYPE_BY_SEVERITY = {"blocker": "issue", "warning": "suggestion", "nit": "pedantic"}
SEVERITY_BY_NATIVE_TYPE = {"issue": "blocker", "pedantic": "nit", "praise": "nit"}


def check_tuicr_available():
    run(["tuicr", "--version"])


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


def _target_for_comment(comment):
    nested = comment.get("target") if isinstance(comment.get("target"), dict) else {}
    path = comment.get("path", comment.get("file", nested.get("file")))
    if not isinstance(path, str) or not path:
        path = None
    start = comment.get("start_line", comment.get("line", nested.get("start_line", nested.get("line"))))
    end = comment.get("end_line", nested.get("end_line"))
    if start is not None and end is None:
        end = start
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        start = None
        end = None
    elif not isinstance(end, int) or isinstance(end, bool) or end < start:
        end = start
    side = comment.get("side", nested.get("side"))
    if side not in ("old", "new"):
        side = "new"
    location = comment.get("location")
    if not isinstance(location, str) or not location:
        if path is None:
            location = "review"
        elif start is None:
            location = path
        elif end == start:
            location = "%s:%d" % (path, start)
        else:
            location = "%s:%d-%d" % (path, start, end)
    return {"path": path, "start": start, "end": end, "side": side, "location": location}


def _native_header(comment):
    author = comment.get("username")
    if not isinstance(author, str) or not author.strip():
        author = comment.get("author")
    if not isinstance(author, str) or not author.strip():
        author = "Human reviewer"
    else:
        author = author.strip()
    comment_type = comment.get("comment_type", comment.get("type"))
    severity = SEVERITY_BY_NATIVE_TYPE.get(comment_type, "warning")
    return {
        "version": PROTOCOL_VERSION,
        "role": "human",
        "author": author,
        "severity": severity,
        "status": "open",
        "reply_to": None,
    }


def normalize_comments(comments):
    """Classify public tuicr JSON without changing the raw digest contract."""
    normalized = []
    unstructured = []
    malformed = []
    for comment in comments:
        comment_id = comment.get("id")
        content = comment.get("content")
        if not isinstance(comment_id, str) or not comment_id.strip() or not isinstance(content, str) or not content.strip():
            unstructured.append(comment)
            continue
        first = content.splitlines()[0] if content.splitlines() else ""
        metadata = parse_header(comment)
        if first.startswith("@nvim-review"):
            if metadata is None:
                malformed.append(comment)
                continue
            origin = "protocol"
            message = content.split("\n", 1)[1] if "\n" in content else ""
        else:
            origin = "native"
            metadata = _native_header(comment)
            message = content
        comment_type = comment.get("comment_type", comment.get("type"))
        if not isinstance(comment_type, str) or not comment_type:
            comment_type = None
        normalized.append(
            {
                "id": comment_id,
                "origin": origin,
                "message": message,
                "header": metadata,
                "comment_type": comment_type,
                "target": _target_for_comment(comment),
                "raw": comment,
            }
        )
    return {"comments": normalized, "unstructured": unstructured, "malformed": malformed}


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


def add_comment(
    root,
    round_value,
    role,
    author,
    severity,
    status,
    message,
    reply_to=None,
    path=None,
    start=None,
    end=None,
    side=None,
):
    if not isinstance(message, str) or not message.strip():
        raise RoundError("invalid_message", "Comment message must not be empty")
    author = resolve_author(round_value, role, author)
    if reply_to is None and status != "open":
        raise RoundError("invalid_status", "Root comments must start with status open")
    inherited = None
    if reply_to is not None:
        comments = get_comments(root, round_value)
        analysis = normalize_comments(comments)
        target = next((item for item in analysis["comments"] if item["id"] == reply_to), None)
        if target is None:
            raise RoundError(
                "invalid_reply",
                "reply_to must identify a valid native or protocol comment in this round",
                {"reply_to": reply_to},
            )
        inherited = target["target"]
    if path is None and start is None and end is None and inherited is not None:
        path, start, end = inherited["path"], inherited["start"], inherited["end"]
    path, start, end = normalize_target(round_value["repo_root"], path, start, end)
    if side is None:
        side = inherited["side"] if inherited is not None else "new"
    if side not in ("old", "new"):
        raise RoundError("invalid_target", "Target side must be old or new", {"side": side})
    content = header(role, author, severity, status, reply_to) + "\n" + message
    payload = {"type": TYPE_BY_SEVERITY[severity], "content": content, "side": side}
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
    analysis = normalize_comments(comments)
    parsed = analysis["comments"]
    unstructured = analysis["unstructured"]
    malformed = list(analysis["malformed"])
    by_id = {}
    for record in parsed:
        by_id[record["id"]] = record

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
    for record in parsed:
        root = root_id(record)
        if root is None:
            malformed.append(record["raw"])
            continue
        groups.setdefault(root, []).append(record)
    threads = []
    for root, records in groups.items():
        latest = records[-1]
        threads.append(
            {
                "root_id": root,
                "status": latest["header"]["status"],
                "severity": by_id[root]["header"]["severity"],
                "comment": by_id[root]["raw"],
                "root": by_id[root],
                "latest_id": latest["id"],
                "latest": latest,
                "latest_role": latest["header"]["role"],
                "latest_author": latest["header"]["author"],
                "comment_ids": [item["id"] for item in records],
            }
        )
    threads.sort(key=lambda item: item["root_id"])
    return {"comments": parsed, "threads": threads, "unstructured": unstructured, "malformed": malformed}


def comment_digest(comments):
    canonical = json.dumps(comments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def confirmation_token(round_id, digest):
    return hashlib.sha256(("nvim-review-close\0" + round_id + "\0" + digest).encode("ascii")).hexdigest()[:32]
