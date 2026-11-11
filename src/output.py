"""Writes the pipeline's final state to a JSON output file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_output(path: str | Path, state: dict[str, Any]) -> None:
    """Write the pipeline's final state as JSON to `path`, creating any
    missing parent directories."""
    config = state["config"]
    payload = {
        "mode": config.mode,
        "result": state.get("structured_result"),
        "raw_response": state.get("raw_response") or None,
        "parse_error": state.get("parse_error"),
        "model": config.llm.model,
        "backend": config.agent.backend,
        "started_at": state.get("started_at"),
        "ended_at": state.get("ended_at"),
        "seconds": state.get("seconds"),
        "usage": state.get("usage"),
    }
    if state.get("rate_limit"):
        payload["rate_limit"] = state["rate_limit"]
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # the exact prompt that was sent, next to the output (identical across runs of a scenario, so kept out of output.json)
    if state.get("messages"):
        system_text, human_text = state["messages"]
        (output_path.parent / "prompt_system.txt").write_text(system_text, encoding="utf-8")
        (output_path.parent / "prompt_user.txt").write_text(human_text, encoding="utf-8")
