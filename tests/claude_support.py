import json
from pathlib import Path


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _usage(*, input_tokens: int = 1, output_tokens: int = 1) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _assistant(
    message_id: str | None,
    uuid: str,
    *,
    session_id: str = "test-session",
    usage: object | None = None,
    iterations: list[dict[str, object]] | None = None,
    sidechain: bool = False,
) -> dict[str, object]:
    usage_value = _usage() if usage is None else usage
    if iterations is not None and isinstance(usage_value, dict):
        usage_value = {**usage_value, "iterations": iterations}
    message: dict[str, object] = {
        "model": "base-model",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "PRIVATE_SENTINEL"}],
        "usage": usage_value,
    }
    if message_id is not None:
        message["id"] = message_id
    return {
        "timestamp": "2026-08-28T00:00:00Z",
        "type": "assistant",
        "sessionId": session_id,
        "uuid": uuid,
        "requestId": f"req-{uuid}",
        "version": "2.1.232",
        "entrypoint": "cli",
        "isSidechain": sidechain,
        "effort": "high",
        "message": message,
    }
