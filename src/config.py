"""Loading and validation for the project's YAML config file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

Mode = Literal["detect", "review"]
Provider = Literal["ollama", "gemini", "openrouter"]


class LLMConfig(BaseModel):
    provider: Provider
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None

    @model_validator(mode="after")
    def _check_api_key_env(self) -> "LLMConfig":
        if self.provider in ("gemini", "openrouter") and not self.api_key_env:
            raise ValueError(
                f"llm.api_key_env is required for provider '{self.provider}'"
            )
        return self


class InputConfig(BaseModel):
    diff_file: Path
    modified_lines_file: Path
    dependencies_file: Optional[Path] = None
    # Checkout of the merged codebase the diff applies to. When set, the
    # review agent's read/grep/glob tools are scoped to this directory so it
    # can open the full source files the diff only shows hunks of, instead
    # of working from the annotated diff text alone.
    source_root: Optional[Path] = None


class OutputConfig(BaseModel):
    path: Path


class AgentConfig(BaseModel):
    """Connection settings for the opencode server that runs the review agents."""

    base_url: str = "http://localhost:4096"
    # If no server is reachable at base_url, spawn `opencode serve` ourselves.
    auto_start: bool = True
    startup_timeout: float = 30.0
    request_timeout: float = 600.0


class DependencyTypeConfig(BaseModel):
    name: str
    detect: bool = True
    definition: str


class AppConfig(BaseModel):
    mode: Mode
    llm: LLMConfig
    input: InputConfig
    output: OutputConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    dependency_types: list[DependencyTypeConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_empty_agent_block(cls, data: object) -> object:
        # An `agent:` key present but with no sub-keys parses as YAML null;
        # treat that the same as the key being absent entirely.
        if isinstance(data, dict) and data.get("agent") is None:
            data = {**data, "agent": {}}
        return data

    @model_validator(mode="after")
    def _check_review_mode_requirements(self) -> "AppConfig":
        if self.mode == "review" and self.input.dependencies_file is None:
            raise ValueError(
                "input.dependencies_file is required when mode is 'review'"
            )
        return self


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate the YAML config file at `config_path`."""
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)
