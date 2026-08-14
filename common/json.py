"""JSON helpers — mirrors new-api's common/json.go rule.

All JSON marshal/unmarshal in business code MUST go through these wrappers.
Direct json.dumps/json.loads is forbidden except inside this module.
"""
from __future__ import annotations

import json
from typing import Any


def marshal(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def unmarshal_str(data: str, target_model: type) -> Any:
    """Parse JSON string into a Pydantic model. Use for client-supplied payloads."""
    return target_model.model_validate_json(data)


def decode_stream_to_dicts(stream) -> list[dict]:
    """Decode an SSE-style line stream into JSON objects, ignoring blank lines."""
    out = []
    for line in stream:
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            out.append(json.loads(payload))
    return out