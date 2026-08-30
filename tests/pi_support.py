import json
from pathlib import Path

def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _header(session_id: str, *, parent: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-08-28T00:00:00Z",
        "cwd": "PRIVATE_DYNAMIC_CWD_SENTINEL",
    }
    if parent is not None:
        value["parentSession"] = parent
    return value


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "totalTokens": input_tokens + output_tokens,
        "cost": {
            "input": input_tokens / 1000,
            "output": output_tokens / 1000,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": (input_tokens + output_tokens) / 1000,
        },
    }


def _assistant(entry_id: str, input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": None,
        "timestamp": "2026-08-28T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "PRIVATE_DYNAMIC_ASSISTANT"}],
            "api": "responses",
            "provider": "test-provider",
            "model": "test-model",
            "responseId": f"response-{entry_id}",
            "usage": _usage(input_tokens, output_tokens),
            "stopReason": "stop",
            "timestamp": 1787875201000,
        },
    }


def _subagent_snapshot(
    entry_id: str,
    *,
    status: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    timestamp: str,
    model: str,
    finished_at: int | None = None,
    thinking_level: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "A001",
        "batchId": "batch-replaced",
        "status": status,
        "startedAt": 1787875200000,
        "model": model,
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": 1,
            "cacheWrite": 0,
            "cost": cost,
        },
    }
    if finished_at is not None:
        data["finishedAt"] = finished_at
    if thinking_level is not None:
        data["thinkingLevel"] = thinking_level
    return {
        "type": "custom",
        "id": entry_id,
        "timestamp": timestamp,
        "customType": "orchestrator-subagent",
        "data": data,
    }
