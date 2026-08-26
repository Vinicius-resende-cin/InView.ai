"""LangGraph pipeline: load inputs -> build prompt -> call LLM -> save output."""

from __future__ import annotations

import json
from typing import Any, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from src.config import AppConfig
from src.llm_providers import build_chat_model
from src.output import write_output
from src.parsing.annotate import AnnotatedFile, annotate_diff_files
from src.parsing.dependency_parser import DependencySet, load_dependencies
from src.parsing.diff_parser import load_diff
from src.parsing.modified_lines_parser import load_modified_lines
from src.prompts.mode_detect import build_detect_messages
from src.prompts.mode_review import build_review_messages
from src.schemas import Mode1Result, Mode2Result


class GraphState(TypedDict, total=False):
    config: AppConfig
    annotated_diff: list[AnnotatedFile]
    dependencies: Optional[DependencySet]
    messages: list[BaseMessage]
    structured_result: Optional[dict]
    raw_response: Optional[str]
    parse_error: Optional[str]


def _result_model_for_mode(mode: str) -> type[BaseModel]:
    return Mode1Result if mode == "detect" else Mode2Result


def _extract_json_object(text: str) -> dict:
    """Find and parse the first top-level JSON object in `text`."""
    start = text.index("{")
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    return obj


def load_inputs(state: GraphState) -> dict:
    config = state["config"]
    authorship_by_class = load_modified_lines(config.input.modified_lines_file)
    diff_files = load_diff(config.input.diff_file)
    annotated_diff = annotate_diff_files(diff_files, authorship_by_class)

    dependencies = None
    if config.mode == "review":
        dependencies = load_dependencies(
            config.input.dependencies_file, authorship_by_class
        )
    return {"annotated_diff": annotated_diff, "dependencies": dependencies}


def build_prompt(state: GraphState) -> dict:
    config = state["config"]
    if config.mode == "detect":
        messages = build_detect_messages(config.dependency_types, state["annotated_diff"])
    else:
        messages = build_review_messages(
            config.dependency_types, state["annotated_diff"], state["dependencies"]
        )
    return {"messages": messages}


def call_llm(state: GraphState) -> dict:
    config = state["config"]
    messages = state["messages"]
    result_model = _result_model_for_mode(config.mode)
    model = build_chat_model(config.llm)

    try:
        structured_model = model.with_structured_output(result_model)
        result = structured_model.invoke(messages)
        return {
            "structured_result": result.model_dump(),
            "raw_response": None,
            "parse_error": None,
        }
    except Exception as structured_exc:
        response = model.invoke(messages)
        raw_text = getattr(response, "content", str(response))
        try:
            parsed = result_model.model_validate(_extract_json_object(raw_text))
            return {
                "structured_result": parsed.model_dump(),
                "raw_response": raw_text,
                "parse_error": None,
            }
        except Exception as parse_exc:
            return {
                "structured_result": None,
                "raw_response": raw_text,
                "parse_error": (
                    f"structured_output failed ({type(structured_exc).__name__}: "
                    f"{structured_exc}); fallback JSON parse also failed "
                    f"({type(parse_exc).__name__}: {parse_exc})"
                ),
            }


def save_output(state: GraphState) -> dict:
    write_output(state["config"].output.path, state)
    return {}


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("load_inputs", load_inputs)
    graph.add_node("build_prompt", build_prompt)
    graph.add_node("call_llm", call_llm)
    graph.add_node("save_output", save_output)

    graph.set_entry_point("load_inputs")
    graph.add_edge("load_inputs", "build_prompt")
    graph.add_edge("build_prompt", "call_llm")
    graph.add_edge("call_llm", "save_output")
    graph.add_edge("save_output", END)

    return graph.compile()


def run_pipeline(config: AppConfig) -> dict[str, Any]:
    graph = build_graph()
    return graph.invoke({"config": config})
