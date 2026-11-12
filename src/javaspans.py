"""Locates methods in Java sources: the line range of every method/constructor, and the
ones a labeled method of the dataset ("Class.method(Type, ...)") refers to."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import javalang
from javalang.tokenizer import tokenize

from src.analysis.common import outer_simple

CONSTRUCTOR_NAMES = {"constructor", "contructor"}  # the dataset misspells it


@dataclass
class MethodSpan:
    name: str  # '<init>' for constructors
    param_types: list[str]
    enclosing_types: list[str]  # outermost first
    start: int
    end: int


def _simple_type(text: str) -> str:
    """'java.util.List<String>...' -> 'List': generics, varargs, package dropped."""
    text = re.sub(r"<.*>", "", text).replace("...", "").replace("[]", "").strip()
    return text.rsplit(".", 1)[-1]


def parse_declaration(declaration: str) -> tuple[str, list[str]]:
    """'decode(ChannelHandlerContext, Channel)' -> ('decode', ['ChannelHandlerContext', 'Channel'])."""
    name, _, rest = declaration.partition("(")
    params_text = rest.rsplit(")", 1)[0]
    params: list[str] = []
    depth, current = 0, ""
    for ch in params_text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            params.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        params.append(current)
    return name.strip(), [_simple_type(p.rsplit(" ", 1)[0] if " " in p.strip() else p) for p in params]


def _end_line(tokens: list, name_index: int) -> int:
    """Last line of the member whose name token is at `name_index`: the
    closing brace of its body, or the ';' of a bodiless declaration."""
    i = name_index
    depth = 0
    while i < len(tokens):  # skip the parameter list
        value = tokens[i].value
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    while i < len(tokens) and tokens[i].value not in ("{", ";"):  # throws ..., default ...
        i += 1
    if i >= len(tokens) or tokens[i].value == ";":
        return tokens[min(i, len(tokens) - 1)].position[0]
    depth = 0
    while i < len(tokens):
        value = tokens[i].value
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return tokens[i].position[0]
        i += 1
    return tokens[-1].position[0]


def index_methods(source: str) -> list[MethodSpan]:
    """Every method/constructor in `source` (nested and anonymous ones too)
    with its line range."""
    tokens = list(tokenize(source))
    tree = javalang.parse.parse(source)
    spans: list[MethodSpan] = []
    for path, node in tree:
        if not isinstance(node, (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration)):
            continue
        if node.position is None:
            continue
        line = node.position[0]
        name_index = next(
            (
                idx
                for idx, tok in enumerate(tokens)
                if tok.position[0] >= line
                and tok.value == node.name
                and idx + 1 < len(tokens)
                and tokens[idx + 1].value == "("
            ),
            None,
        )
        if name_index is None:
            continue
        # javalang's path interleaves ancestor nodes with the lists holding
        # them (whose entries are siblings, not ancestors): keep only nodes.
        enclosing = [n.name for n in path if isinstance(n, javalang.tree.TypeDeclaration)]
        spans.append(
            MethodSpan(
                name="<init>" if isinstance(node, javalang.tree.ConstructorDeclaration) else node.name,
                param_types=[_simple_type(p.type.name) for p in node.parameters],
                enclosing_types=enclosing,
                start=min(line, tokens[name_index].position[0]),
                end=_end_line(tokens, name_index),
            )
        )
    return spans


def class_source_path(scenario_dir: Path, class_name: str) -> Path:
    outer = class_name.split("$")[0]
    return scenario_dir / "source" / "src" / "main" / "java" / (outer.replace(".", "/") + ".java")


def find_method(spans: list[MethodSpan], class_name: str, declaration: str) -> list[MethodSpan]:
    """Spans matching a labeled method: by name and arity, narrowed by
    parameter types when that disambiguates overloads."""
    simple_class = outer_simple(class_name)
    inner = class_name.split("$", 1)[1].split("$")[-1] if "$" in class_name else None
    name, params = parse_declaration(declaration)
    if name in CONSTRUCTOR_NAMES or name == simple_class:
        name = "<init>"

    def in_class(span: MethodSpan) -> bool:
        if inner is not None:
            return inner in span.enclosing_types
        return span.enclosing_types[:1] == [simple_class] and len(span.enclosing_types) == 1

    candidates = [s for s in spans if s.name == name and len(s.param_types) == len(params) and in_class(s)]
    if not candidates and inner is None:
        # The dataset sometimes names the outer class for a method that lives
        # in a nested one: fall back to any class of the file.
        candidates = [s for s in spans if s.name == name and len(s.param_types) == len(params)]
    exact = [s for s in candidates if s.param_types == params]
    return exact or candidates
