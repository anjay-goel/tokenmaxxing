import json
from collections.abc import Mapping
from typing import cast

from tokenmaxxing.models import JsonValue


class PrivacyError(ValueError):
    """Raised when a projection would cross the metadata-only boundary."""


_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "prompt",
        "prompts",
        "assistant",
        "assistanttext",
        "reasoning",
        "reasoningtext",
        "summary",
        "summaries",
        "command",
        "commands",
        "arguments",
        "argument",
        "toolarguments",
        "toolargument",
        "results",
        "result",
        "toolresults",
        "toolresult",
        "rawerror",
        "rawerrors",
        "attachment",
        "attachments",
        "rawapi",
        "rawapibody",
        "apibody",
        "cwd",
        "rawcwd",
        "workingdirectory",
    }
)
_ALLOWED_PROJECTION_KEYS = frozenset({"usage"})
_PI_SUBAGENT_METADATA_FIELDS = frozenset({"provider", "model", "effort"})


def _normalized_key(key: str) -> str:
    return key.lower().replace("_", "")


def _validate_key(key: str, path: str) -> None:
    if _normalized_key(key) in _FORBIDDEN_KEYS:
        raise PrivacyError(f"forbidden projection field at {path}")


def _usage_value(value: object, path: str) -> JsonValue | object:
    if path.startswith("usage._pi."):
        key = path.rpartition(".")[2]
        if key in _PI_SUBAGENT_METADATA_FIELDS and isinstance(value, str):
            return cast(JsonValue, value)
    if isinstance(value, (bool, int, float)):
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise PrivacyError(f"projection key at {path} must be a string")
            nested_path = f"{path}.{key}"
            if _normalized_key(key) == "reasoning":
                if isinstance(nested_value, bool) or not isinstance(
                    nested_value, (int, float)
                ):
                    raise PrivacyError(f"reasoning usage must be numeric at {nested_path}")
            else:
                _validate_key(key, nested_path)
            retained = _usage_value(nested_value, nested_path)
            if retained is not _DROP:
                result[key] = cast(JsonValue, retained)
        return result
    return _DROP


_DROP = object()


def validate_projection(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, nested_value in value.items():
        if not isinstance(key, str):
            raise PrivacyError("projection keys must be strings")
        _validate_key(key, key)
        normalized_key = _normalized_key(key)
        if normalized_key not in _ALLOWED_PROJECTION_KEYS:
            raise PrivacyError(f"projection field at {key} is not allowlisted")
        if normalized_key == "usage":
            retained = _usage_value(nested_value, key)
            if retained is _DROP:
                raise PrivacyError("usage must be a JSON object or scalar numeric value")
            result[key] = cast(JsonValue, retained)
    return result


def projection_json(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        validate_projection(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
