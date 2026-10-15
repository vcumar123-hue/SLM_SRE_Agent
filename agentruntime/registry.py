"""
Tool registry and the tool boundary (design 4.2).

A tool is `fn(ctx, **validated_args) -> list[EvidenceDraft]`. Registration refuses any schema that could let a model steer the tenant,
scope, collection, SQL, path or URL, or that is not strict; calls validate the arguments with a small built-in validator (no external
dependency, and any schema keyword it does not understand is refused), run with a timeout, and fail closed if a result carries a tenant
other than the context's.
"""
from __future__ import annotations

import inspect
import itertools
import re
from typing import Any, Callable, Dict, List, Tuple

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.executor import CallTimeout, CancelToken, ExecutorBusy, ExecutorFailed, TOOL_EXECUTOR, BoundedExecutor

FORBIDDEN_ARG_NAMES = frozenset({
    "tenant", "tenant_id", "client", "client_id", "knowledge_scope", "scope", "search_order", "collection", "collection_name",
    "sql", "query_sql", "statement", "path", "file", "filename", "url", "uri", "endpoint", "host_url", "cancel", "ctx",
})
ALLOWED_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern",
    "description", "default",
})
ALLOWED_TYPES = frozenset({"object", "string", "integer", "number", "boolean"})


class RegistryError(Exception):
    """A tool could not be registered (an unsafe or unsupported schema)."""


class ToolInputError(Exception):
    """The model-supplied arguments did not match the tool's schema."""


class UnknownTool(Exception):
    pass


class BoundaryViolation(Exception):
    """A tool returned data that does not belong to the context's tenant, or otherwise broke the boundary."""


class ToolTimeout(Exception):
    pass


class ToolBusy(Exception):
    """Earlier timed-out tool calls are still running; new calls are refused until they finish."""


def check_schema(schema: Dict[str, Any], where: str = "schema") -> None:
    """Refuse schemas that are not strict or that expose a forbidden argument name."""
    if not isinstance(schema, dict):
        raise RegistryError(f"{where}: schema must be an object")
    unknown = set(schema) - ALLOWED_KEYWORDS
    if unknown:
        raise RegistryError(f"{where}: unsupported schema keyword(s) {sorted(unknown)}")
    if schema.get("type") not in ALLOWED_TYPES:
        raise RegistryError(f"{where}: a supported 'type' is required")
    if schema["type"] == "object":
        if schema.get("additionalProperties") is not False:
            raise RegistryError(f"{where}: an object schema must set additionalProperties to false")
        for name, sub in (schema.get("properties") or {}).items():
            if str(name).lower() in FORBIDDEN_ARG_NAMES:
                raise RegistryError(f"{where}: argument '{name}' is not allowed (the server fixes it)")
            check_schema(sub, f"{where}.{name}")


def validate_args(value: Any, schema: Dict[str, Any], where: str = "input") -> None:
    """Validate against the supported schema subset; raise ToolInputError with a short reason."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolInputError(f"{where}: expected an object")
        props = schema.get("properties") or {}
        extra = set(value) - set(props)
        if extra:
            raise ToolInputError(f"{where}: unexpected argument(s) {sorted(extra)}")
        for name in schema.get("required") or []:
            if name not in value:
                raise ToolInputError(f"{where}: missing required argument '{name}'")
        for name, item in value.items():
            validate_args(item, props[name], f"{where}.{name}")
        return
    if kind == "string":
        if not isinstance(value, str):
            raise ToolInputError(f"{where}: expected a string")
        if len(value) > schema.get("maxLength", 10**9):
            raise ToolInputError(f"{where}: longer than {schema['maxLength']} characters")
        if len(value) < schema.get("minLength", 0):
            raise ToolInputError(f"{where}: shorter than {schema['minLength']} characters")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ToolInputError(f"{where}: does not match the allowed pattern")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolInputError(f"{where}: expected an integer")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolInputError(f"{where}: expected a number")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolInputError(f"{where}: expected a boolean")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{where}: not one of the allowed values")
    if kind in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{where}: above {schema['maximum']}")


class ToolRegistry:
    def __init__(self, executor: BoundedExecutor = TOOL_EXECUTOR) -> None:
        self._tools: Dict[str, Tuple[ToolSpec, Callable[..., List[EvidenceDraft]]]] = {}
        self._executor = executor

    def register(self, spec: ToolSpec, fn: Callable[..., List[EvidenceDraft]]) -> None:
        if spec.tier != "read":
            raise RegistryError("only read tools exist in Release 2")
        if spec.name in self._tools:
            raise RegistryError(f"tool '{spec.name}' is already registered")
        check_schema(spec.input_schema, f"tool '{spec.name}'")
        if spec.input_schema.get("type") != "object":
            raise RegistryError(f"tool '{spec.name}': the input schema must describe an object")
        self._tools[spec.name] = (spec, fn)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise UnknownTool(name)
        return self._tools[name][0]

    def versions(self) -> Dict[str, str]:
        return {name: spec.version for name, (spec, _fn) in sorted(self._tools.items())}

    def describe(self, allowed: List[str]) -> List[Dict[str, Any]]:
        """What a planner may see: name, description and argument schema of the allowed tools only."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for name, (s, _f) in sorted(self._tools.items()) if name in allowed
        ]

    def call(self, ctx: TrustedContext, name: str, args: Dict[str, Any], timeout_s: float) -> List[EvidenceDraft]:
        """Validate, run with a timeout, and enforce the tenant boundary on the result."""
        if name not in self._tools:
            raise UnknownTool(name)
        spec, fn = self._tools[name]
        validate_args(args, spec.input_schema, f"tool '{name}' input")
        accepts_cancel = "cancel" in inspect.signature(fn).parameters
        limit = spec.max_items

        def job(token: CancelToken) -> List[Any]:
            kwargs = dict(args)
            if accepts_cancel:
                kwargs["cancel"] = token          # cooperative cancellation; wrappers also set database statement timeouts
            produced = fn(ctx, **kwargs)
            # bounded iteration inside the worker: a huge or endless generator is never fully materialised, and the timeout covers it
            return list(itertools.islice(iter(produced or ()), limit))

        try:
            raw = self._executor.run(job, timeout_s)
        except CallTimeout as exc:
            raise ToolTimeout(f"tool '{name}' exceeded {timeout_s:.0f}s") from exc
        except (ExecutorBusy, ExecutorFailed) as exc:
            raise ToolBusy(str(exc)) from exc
        drafts: List[EvidenceDraft] = []
        for item in raw:
            draft = item if isinstance(item, EvidenceDraft) else EvidenceDraft(**item)
            if draft.tenant_id != ctx.tenant_id:
                raise BoundaryViolation(f"tool '{name}' returned a record for another tenant")
            drafts.append(draft.model_copy(update={"tool": name, "tool_version": spec.version}))
        return drafts
