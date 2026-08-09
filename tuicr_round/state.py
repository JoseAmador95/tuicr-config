"""Round state layout, resolution, and permission handling."""

import os
import pathlib
import uuid

from .util import RoundError, atomic_json, ensure_dir, file_lock, read_json


STATE_ENV = "NVIM_REVIEW_STATE_HOME"


def state_root(explicit=None):
    if explicit:
        root = pathlib.Path(explicit)
    elif os.environ.get(STATE_ENV):
        root = pathlib.Path(os.environ[STATE_ENV])
    elif os.environ.get("XDG_STATE_HOME"):
        root = pathlib.Path(os.environ["XDG_STATE_HOME"]) / "nvim-review"
    else:
        root = pathlib.Path.home() / ".local" / "state" / "nvim-review"
    return root.expanduser().resolve()


def prepare_root(root):
    root = ensure_dir(root)
    ensure_dir(root / "rounds")
    return root


def round_dir(root, round_id):
    return pathlib.Path(root) / "rounds" / str(round_id)


def round_file(root, round_id):
    return round_dir(root, round_id) / "round.json"


def validate_round_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise RoundError("invalid_round", "Round id must be a UUID", {"round": value})


def load_round(root, round_id):
    normalized = validate_round_id(round_id)
    path = round_file(root, normalized)
    if not path.is_file():
        raise RoundError("round_not_found", "Round was not found", {"round": normalized})
    value = read_json(path)
    if value.get("id") != normalized:
        raise RoundError("invalid_state", "Round id does not match its state file", {"round": normalized})
    return value


def save_round(root, value):
    atomic_json(round_file(root, value["id"]), value)


def update_round(root, round_id, changes):
    with round_lock(root, round_id):
        latest = load_round(root, round_id)
        latest.update(changes)
        save_round(root, latest)
        return latest


def iter_rounds(root):
    rounds = pathlib.Path(root) / "rounds"
    if not rounds.is_dir():
        return
    for path in sorted(rounds.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        state_path = path / "round.json"
        if not state_path.is_file():
            continue
        try:
            value = read_json(state_path)
        except RoundError:
            continue
        if value.get("id") == path.name:
            yield value


def resolve_round(root, round_id=None, repo=None, require_open=True):
    if round_id:
        value = load_round(root, round_id)
        if repo is not None and pathlib.Path(value["repo_root"]) != pathlib.Path(repo).expanduser().resolve():
            raise RoundError("round_repo_mismatch", "Round does not belong to the selected repository")
        candidates = [value]
    else:
        if repo is None:
            raise RoundError("missing_selector", "Specify --round or --repo")
        wanted = str(pathlib.Path(repo).expanduser().resolve())
        candidates = [value for value in iter_rounds(root) if value.get("repo_root") == wanted]
    if require_open:
        candidates = [value for value in candidates if not value.get("closed_at")]
    if not candidates:
        raise RoundError("round_not_found", "No matching round was found")
    if len(candidates) != 1:
        raise RoundError(
            "ambiguous_round",
            "More than one round matches; specify --round",
            {"rounds": sorted(value["id"] for value in candidates)},
        )
    return candidates[0]


def round_lock(root, round_id, name="round"):
    return file_lock(round_dir(root, round_id) / (name + ".lock"))
