"""Command line interface for isolated, synthetic tuicr review rounds."""

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sys

from .git_baseline import resolve_repo, start_rounds
from .protocol import (
    add_comment,
    analyze_threads,
    check_tuicr_available,
    comment_digest,
    comments_for_session,
    confirmation_token,
    get_comments,
    require_session,
    resolve_session,
)
from .state import iter_rounds, load_round, prepare_root, resolve_round, round_dir, round_lock, save_round, state_root
from .tmux_control import ensure_session, launch_tuicr, stop_server, tui_active
from .util import RoundError, emit, isoformat, parse_time, process_alive, utc_now


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise RoundError("invalid_arguments", message)


def _parser():
    parser = JsonArgumentParser(prog="tuicr-round", description="Isolated synthetic review rounds for tuicr")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    start = commands.add_parser("start", help="capture the current repository state")
    start.add_argument("--repo", default=".")
    start.add_argument("--open", action="store_true", dest="open_after_start")

    def selector(command):
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--round")
        group.add_argument("--repo")

    opened = commands.add_parser("open", help="open or attach to the round's private TUI")
    selector(opened)
    status = commands.add_parser("status", help="show round and exact tuicr session state")
    selector(status)

    def comment_arguments(command, response=False):
        selector(command)
        command.add_argument("--role", choices=("human", "agent", "verifier"), default="human")
        command.add_argument("--author")
        command.add_argument("--severity", choices=("blocker", "warning", "nit"), required=True)
        command.add_argument("--status", choices=("open", "accept", "discuss", "reject"), default="discuss" if response else "open")
        if response:
            command.add_argument("--reply-to", required=True)
        command.add_argument("--path")
        command.add_argument("--start", type=int)
        command.add_argument("--end", type=int)
        command.add_argument("message")

    add = commands.add_parser("add", help="add a structured root comment")
    comment_arguments(add)
    respond = commands.add_parser("respond", help="add a structured response")
    comment_arguments(respond, response=True)
    accepted = commands.add_parser("accepted", help="list threads whose latest response accepts them")
    selector(accepted)
    close = commands.add_parser("close", help="manually close a round after protocol checks")
    selector(close)
    close.add_argument("--confirm")
    commands.add_parser("clean", help="remove closed inactive rounds older than 30 days")
    return parser


def _selected(root, arguments, require_open=True):
    repo = resolve_repo(arguments.repo) if getattr(arguments, "repo", None) else None
    return resolve_round(root, getattr(arguments, "round", None), repo, require_open=require_open)


def _xdg_config_home():
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        return pathlib.Path(configured).expanduser().resolve()
    return pathlib.Path(__file__).resolve().parents[2]


def _command_start(root, arguments):
    parent, created = start_rounds(arguments.repo, root, _xdg_config_home())
    return {
        "ok": True,
        "command": "start",
        "round": parent["id"],
        "repo_root": parent["repo_root"],
        "branch": parent["branch"],
        "head": parent["head"],
        "s0_tree": parent["s0_tree"],
        "s0_commit": parent["s0_commit"],
        "b0_tree": parent["b0_tree"],
        "b0_commit": parent["b0_commit"],
        "review_base_commit": parent["review_base_commit"],
        "clean": parent["clean"],
        "warnings": parent["warnings"],
        "rounds": [{"round": item["id"], "repo_root": item["repo_root"], "parent_round": item["parent_round"]} for item in created],
    }


def _open_value(root, value, entrypoint, payload=None):
    result = ensure_session(root, value, entrypoint)
    payload = payload or {
        "ok": True,
        "command": "open",
        "round": value["id"],
        "repo_root": value["repo_root"],
    }
    payload.update(
        {
            "tmux_session": result["session"],
            "tmux_socket": result["socket"],
            "created": result["created"],
            "attach_argv": result["attach_argv"],
        }
    )
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        emit(payload)
        return None
    # This shared launcher is often entered from a tmux popup or from a
    # Neovim terminal already hosted by the user's main tmux server.  The
    # per-round server is intentionally separate, so remove only the parent
    # tmux routing variables before attaching to it.
    environment = os.environ.copy()
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    os.execvpe(result["attach_argv"][0], result["attach_argv"], environment)
    return None


def _command_open(root, arguments, entrypoint):
    return _open_value(root, _selected(root, arguments), entrypoint)


def _command_status(root, arguments):
    value = _selected(root, arguments)
    session = resolve_session(root, value)
    return {
        "ok": True,
        "command": "status",
        "round": value["id"],
        "repo_root": value["repo_root"],
        "closed_at": value.get("closed_at"),
        "tui_active": tui_active(value),
        "session": session,
    }


def _command_comment(root, arguments, response):
    value = _selected(root, arguments)
    result, author, payload = add_comment(
        root,
        value,
        arguments.role,
        arguments.author,
        arguments.severity,
        arguments.status,
        arguments.message,
        reply_to=arguments.reply_to if response else None,
        path=arguments.path,
        start=arguments.start,
        end=arguments.end,
    )
    response_value = {
        "ok": True,
        "command": "respond" if response else "add",
        "round": value["id"],
        "author": author,
        "tuicr": result,
    }
    if response and arguments.status == "accept":
        command = "tuicr-round accepted --round " + value["id"]
        response_value["warnings"] = [
            {
                "code": "acceptance_requires_explicit_read",
                "message": "Acceptance was recorded; agents must read it with the public accepted command and no keys were sent to a pane.",
                "command": command,
            }
        ]
    return response_value


def _command_accepted(root, arguments):
    value = _selected(root, arguments)
    analysis = analyze_threads(get_comments(root, value))
    accepted = [thread for thread in analysis["threads"] if thread["status"] == "accept"]
    return {
        "ok": True,
        "command": "accepted",
        "round": value["id"],
        "accepted": accepted,
        "unstructured_count": len(analysis["unstructured"]),
        "malformed_count": len(analysis["malformed"]),
    }


def _command_close(root, arguments):
    value = _selected(root, arguments)
    if tui_active(value):
        raise RoundError("active_tui", "The round cannot close while its TUI is active")
    session = resolve_session(root, value)
    if session is not None and session.get("active") is True:
        raise RoundError("active_tui", "The round cannot close while tuicr reports its session active")
    with round_lock(root, value["id"], "protocol"):
        comments = comments_for_session(value, session) if session is not None else []
        analysis = analyze_threads(comments)
        blocking = [thread for thread in analysis["threads"] if thread["status"] in ("open", "discuss")]
        unresolved = {
            "unstructured": len(analysis["unstructured"]),
            "malformed": len(analysis["malformed"]),
            "blocking_threads": [thread["root_id"] for thread in blocking],
        }
        needs_confirmation = bool(unresolved["unstructured"] or unresolved["malformed"] or blocking)
        digest = comment_digest(comments)
        token = confirmation_token(value["id"], digest)
        if needs_confirmation and arguments.confirm is None:
            raise RoundError(
                "confirmation_required",
                "Open, discuss, or unmanaged comments remain; retry with --confirm TOKEN to close manually",
                {"token": token, "comment_digest": digest, **unresolved},
                exit_code=3,
            )
        if arguments.confirm is not None and arguments.confirm != token:
            raise RoundError("stale_confirmation", "Confirmation token is invalid or stale", {"comment_digest": digest})
        with round_lock(root, value["id"]):
            latest = load_round(root, value["id"])
            if latest.get("closed_at"):
                raise RoundError("round_closed", "Round is already closed", {"closed_at": latest["closed_at"]})
            latest["closed_at"] = isoformat()
            latest["close_comment_digest"] = digest
            latest["owner_pid"] = None
            save_round(root, latest)
    stop_server(value)
    return {"ok": True, "command": "close", "round": value["id"], "closed_at": latest["closed_at"]}


def _command_clean(root):
    removed = []
    retained = []
    cutoff = utc_now() - datetime.timedelta(days=30)
    for value in list(iter_rounds(root)):
        reason = None
        if not value.get("closed_at"):
            reason = "open"
        else:
            try:
                closed = parse_time(value["closed_at"])
            except ValueError:
                reason = "invalid_closed_at"
            else:
                if closed >= cutoff:
                    reason = "younger_than_30_days"
        if reason is None and process_alive(value.get("owner_pid")):
            reason = "live_owner"
        if reason is None and tui_active(value):
            reason = "active_tui"
        if reason is not None:
            retained.append({"round": value["id"], "reason": reason})
            continue
        destination = round_dir(root, value["id"])
        shutil.rmtree(str(destination))
        removed.append(value["id"])
    return {"ok": True, "command": "clean", "removed": sorted(removed), "retained": retained, "minimum_age_days": 30}


def _internal(argv):
    parser = JsonArgumentParser(prog="tuicr-round __launch", add_help=False)
    parser.add_argument("--round", required=True)
    parser.add_argument("--state-home", required=True)
    arguments = parser.parse_args(argv)
    root = state_root(arguments.state_home)
    value = load_round(root, arguments.round)
    return launch_tuicr(value)


def main(argv=None, entrypoint=None):
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "__launch":
            return _internal(argv[1:])
        arguments = _parser().parse_args(argv)
        check_tuicr_available()
        root = prepare_root(state_root())
        if arguments.command == "start":
            result = _command_start(root, arguments)
            if arguments.open_after_start:
                value = load_round(root, result["round"])
                return _open_value(root, value, entrypoint or pathlib.Path(sys.argv[0]).resolve(), result)
        elif arguments.command == "open":
            return _command_open(root, arguments, entrypoint or pathlib.Path(sys.argv[0]).resolve())
        elif arguments.command == "status":
            result = _command_status(root, arguments)
        elif arguments.command == "add":
            result = _command_comment(root, arguments, False)
        elif arguments.command == "respond":
            result = _command_comment(root, arguments, True)
        elif arguments.command == "accepted":
            result = _command_accepted(root, arguments)
        elif arguments.command == "close":
            result = _command_close(root, arguments)
        elif arguments.command == "clean":
            result = _command_clean(root)
        else:
            raise RoundError("invalid_arguments", "Unsupported command")
        emit(result)
        return 0
    except RoundError as error:
        emit({"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details or {}}})
        return error.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "interrupted", "message": "Operation interrupted", "details": {}}})
        return 130
    except Exception as error:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected launcher failure",
                    "details": {"type": type(error).__name__, "reason": str(error)},
                },
            }
        )
        return 1
