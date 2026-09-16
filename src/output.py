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
    }
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
