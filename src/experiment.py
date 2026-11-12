"""Running Mode 1 ("detect") with a model given as a `--models` entry of the experiment scripts."""

from __future__ import annotations

import re
from pathlib import Path

from src.config import AppConfig
from src.graph import run_pipeline

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
CLAUDE_PREFIX = "claude:"


def sanitize_model_name(model: str) -> str:
    """Turn a model spec (e.g. 'gpt-oss:20b-cloud', 'claude:sonnet') into a
    filesystem-safe name."""
    return _UNSAFE_CHARS.sub("-", model).strip("-")


def resolve_model_spec(model: str) -> tuple[str, str]:
    """Resolve a `--models` entry into (model_tag, agent.backend).

    A 'claude:<model>' spec (e.g. 'claude:sonnet') routes through the
    claude_code backend (the Claude Code CLI). Anything else runs through
    the opencode backend. llm.provider is left as configured in the base
    config either way.
    """
    if model.startswith(CLAUDE_PREFIX):
        model_tag = model[len(CLAUDE_PREFIX):]
        if not model_tag:
            raise ValueError(f"'{model}' is missing a model name after '{CLAUDE_PREFIX}'")
        return model_tag, "claude_code"
    return model, "opencode"


def run_once(base_config: AppConfig, model: str, output_dir: Path) -> Path:
    """Run the detect-mode pipeline once with `model` substituted in, writing
    output.json under `output_dir`. Returns the output.json path."""
    model_tag, backend = resolve_model_spec(model)
    output_path = output_dir / "output.json"

    run_config = base_config.model_copy(
        deep=True,
        update={
            "mode": "detect",
            "llm": base_config.llm.model_copy(update={"model": model_tag}),
            "agent": base_config.agent.model_copy(update={"backend": backend}),
            "output": base_config.output.model_copy(update={"path": output_path}),
        },
    )
    run_pipeline(run_config)
    return output_path
