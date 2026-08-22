"""One private tmux server/session and one tuicr process per round."""

import os
import pathlib

from .handoff import handoff_marker
from .protocol import check_tuicr_available, prepare_pbcopy_wrapper, tuicr_environment
from .state import round_lock, update_round
from .util import RoundError, emit, run


SESSION_NAME = "nvim-review"


def _base(round_value):
    socket = pathlib.Path(round_value["external_git_dir"]).parent / "tmux" / "socket"
    return ["tmux", "-S", str(socket), "-f", "/dev/null"], socket


def _tmux(round_value, arguments, check=True):
    base, unused = _base(round_value)
    return run(base + list(arguments), check=check)


def session_exists(round_value):
    return _tmux(round_value, ["has-session", "-t", SESSION_NAME], check=False).returncode == 0


def tui_active(round_value):
    if not session_exists(round_value):
        return False
    result = _tmux(round_value, ["display-message", "-p", "-t", SESSION_NAME, "#{pane_dead}"], check=False)
    return result.returncode == 0 and result.stdout.strip() == b"0"


def _server_pid(round_value):
    result = _tmux(round_value, ["display-message", "-p", "-t", SESSION_NAME, "#{pid}"], check=False)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def ensure_session(root, round_value, entrypoint):
    base, socket = _base(round_value)
    with round_lock(root, round_value["id"], "tmux"):
        existed = session_exists(round_value)
        if existed and not tui_active(round_value):
            raise RoundError(
                "tui_exited",
                "The round's tuicr process exited; state and the tmux pane were preserved",
                {"attach": base + ["attach-session", "-t", SESSION_NAME]},
            )
        if not existed:
            prepare_pbcopy_wrapper(round_value)
            status_text = "%s | y: copy review + UUID" % handoff_marker(round_value["id"])
            run(
                base
                + [
                    "start-server",
                    ";",
                    "set-option",
                    "-g",
                    "exit-empty",
                    "off",
                    ";",
                    "set-option",
                    "-g",
                    "status",
                    "on",
                    ";",
                    "set-option",
                    "-g",
                    "status-format[0]",
                    status_text,
                    ";",
                    "set-option",
                    "-g",
                    "prefix",
                    "None",
                    ";",
                    "set-option",
                    "-g",
                    "prefix2",
                    "None",
                    ";",
                    "set-window-option",
                    "-g",
                    "remain-on-exit",
                    "failed",
                ]
            )
            command = [
                str(entrypoint),
                "__launch",
                "--round",
                round_value["id"],
                "--state-home",
                str(root),
            ]
            try:
                run(base + ["new-session", "-d", "-s", SESSION_NAME, "-x", "120", "-y", "40"] + command)
                # Bootstrap needs exit-empty=off because no session exists yet.
                # Once the TUI session is present, restore the normal policy:
                # successful exits remove the last session/server and release
                # every attach client, while remain-on-exit=failed preserves
                # nonzero output for diagnosis.
                run(base + ["set-option", "-g", "exit-empty", "on"])
            except RoundError:
                run(base + ["kill-server"], check=False)
                raise
            round_value["owner_pid"] = _server_pid(round_value)
            update_round(root, round_value["id"], {"owner_pid": round_value["owner_pid"]})
        attach = base + ["attach-session", "-t", SESSION_NAME]
        return {"created": not existed, "socket": str(socket), "session": SESSION_NAME, "attach_argv": attach}


def stop_server(round_value):
    if session_exists(round_value):
        _tmux(round_value, ["kill-server"], check=False)


def launch_tuicr(round_value):
    try:
        check_tuicr_available()
        environment = tuicr_environment(round_value)
        arguments = [
            "tuicr",
            "-r",
            round_value["review_base_commit"] + ".." + round_value["b0_commit"],
        ]
        arguments.append("--no-update-check")
        os.chdir(round_value["repo_root"])
        os.execvpe(arguments[0], arguments, environment)
    except RoundError as error:
        emit({"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details or {}}})
        return error.exit_code
    except Exception as error:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected internal launcher failure",
                    "details": {"type": type(error).__name__, "reason": str(error)},
                },
            }
        )
        return 1
