"""Surface mapping: turn an OpenAPI spec into the operation list the planner
walks. Supports YAML (VAmPI) and JSON (crAPI). No network — reads the vendored
spec file so a run is reproducible offline.

An operation that carries a path parameter (e.g. /books/v1/{book_title},
/vehicle/{id}) is an *object reference*: the BOLA candidate set, where the
methodology is "same identity, vary the object, diff the responses".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_METHODS = ("get", "post", "put", "delete", "patch")


@dataclass
class Operation:
    method: str
    path: str
    operation_id: str
    summary: str = ""
    path_params: list[str] = field(default_factory=list)
    secured: bool = False          # declares a security requirement
    schema_source: str = "openapi"

    @property
    def is_object_ref(self) -> bool:
        return bool(self.path_params)

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.path}"


def load_spec(path: str | Path) -> dict:
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yml", ".yaml"):
        return yaml.safe_load(text)
    return json.loads(text)


def map_operations(spec: dict) -> list[Operation]:
    ops: list[Operation] = []
    global_security = bool(spec.get("security"))
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", [])
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            params = list(shared_params) + list(op.get("parameters", []))
            path_params = [
                prm.get("name")
                for prm in params
                if isinstance(prm, dict) and prm.get("in") == "path" and prm.get("name")
            ]
            secured = global_security or ("security" in op)
            ops.append(
                Operation(
                    method=method.lower(),
                    path=path,
                    operation_id=op.get("operationId") or f"{method}_{path}",
                    summary=op.get("summary", ""),
                    path_params=path_params,
                    secured=secured,
                )
            )
    return ops
