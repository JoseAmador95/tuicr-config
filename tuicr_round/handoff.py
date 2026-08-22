"""Stable handoff text shared by the launcher and exported reviews."""


def handoff_marker(round_id: str) -> str:
    """Return the stable marker for a review round."""
    return "TUICR-ROUND:" + round_id


def handoff_prompt(round_id: str) -> str:
    """Return the canonical instruction pasted with a review."""
    return "Use $tuicr-address-review to process %s." % handoff_marker(round_id)


def handoff_fields(round_id: str) -> dict:
    """Return the handoff metadata included in launcher responses."""
    return {"handoff": handoff_marker(round_id), "prompt": handoff_prompt(round_id)}
