"""Factory that turns an LLMConfig into a configured LangChain chat model."""

from __future__ import annotations

import os

from src.config import LLMConfig


def _resolve_api_key(config: LLMConfig) -> str:
    assert config.api_key_env is not None
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Environment variable '{config.api_key_env}' is not set "
            f"(required for llm.provider '{config.provider}')"
        )
    return api_key


def build_chat_model(config: LLMConfig):
    """Build a LangChain chat model for the given provider config."""
    if config.provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs = {
            "model": config.model,
            "temperature": config.temperature,
            "num_predict": config.max_tokens,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOllama(**kwargs)

    if config.provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=config.model,
            temperature=config.temperature,
            max_output_tokens=config.max_tokens,
            google_api_key=_resolve_api_key(config),
        )

    if config.provider == "openrouter":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=_resolve_api_key(config),
            base_url=config.base_url or "https://openrouter.ai/api/v1",
        )

    raise ValueError(f"Unsupported llm.provider: {config.provider!r}")
