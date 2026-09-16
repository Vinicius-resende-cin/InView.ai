"""Command-line entry point: `python -m src.cli --config config.yaml`."""

from __future__ import annotations

import argparse
import sys

from src.config import load_config
from src.graph import run_pipeline


def main(argv: list[str] | None = None) -> int:
    """Load the config, run the pipeline, and report the result.

    Returns 1 (and prints a warning to stderr) if the run ended with a
    parse_error, 0 otherwise.
    """
    parser = argparse.ArgumentParser(
        description="Run an LLM code review (Mode 1: detect, Mode 2: review) "
        "on a merge scenario diff, as configured in a YAML config file."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file (default: config.yaml).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    state = run_pipeline(config)

    print(f"Mode: {config.mode}")
    print(f"Output written to: {config.output.path}")
    if state.get("parse_error"):
        print(f"Warning: {state['parse_error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
