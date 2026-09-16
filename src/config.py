"""Loading and validation for the project's YAML config file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

Mode = Literal["detect", "review"]
Provider = Literal["ollama", "gemini", "openrouter", "anthropic"]

# Providers needing a pay-per-token API key, as opposed to "ollama" (local
# server). See README's "Agent backends" section for how each is authenticated.
_PROVIDERS_REQUIRING_API_KEY = ("gemini", "openrouter", "anthropic")


class LLMConfig(BaseModel):
    """Which model to use and, for API-key providers, how to authenticate."""

    provider: Provider
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None

    @model_validator(mode="after")
    def _check_api_key_env(self) -> "LLMConfig":
        if self.provider in _PROVIDERS_REQUIRING_API_KEY and not self.api_key_env:
            raise ValueError(
                f"llm.api_key_env is required for provider '{self.provider}'"
            )
        return self


class InputConfig(BaseModel):
    """Paths to the merge scenario's diff, authorship, and (Mode 2) the
    static-analysis tool's findings."""

    diff_file: Path
    modified_lines_file: Path
    dependencies_file: Optional[Path] = None
    source_root: Optional[Path] = None


class OutputConfig(BaseModel):
    """Where to write the pipeline's result JSON."""

    path: Path


Backend = Literal["opencode", "claude_code"]


class AgentConfig(BaseModel):
    """Settings for the agent backend that runs the review agents.

    See README's "Agent backends" section for what each backend is and why
    both exist.
    """

    backend: Backend = "opencode"

    # -- opencode backend only --
    base_url: str = "http://localhost:4096"
    auto_start: bool = True
    startup_timeout: float = 30.0
    format_retry_count: int = 3

    # -- shared by both backends --
    request_timeout: float = 600.0
    max_retries: int = 2


class DependencyTypeConfig(BaseModel):
    """One semantic dependency type: its definition text for the prompt, and
    whether Mode 1 should actively look for it."""

    name: str
    detect: bool = True
    definition: str


class AppConfig(BaseModel):
    """Root config schema, loaded from config.yaml."""

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
