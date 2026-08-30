import pytest

from tokenmaxxing.privacy import PrivacyError, projection_json, validate_projection


def test_projection_rejects_content_but_keeps_new_numeric_usage() -> None:
    assert validate_projection({"usage": {"new_counter": 7}}) == {
        "usage": {"new_counter": 7}
    }
    with pytest.raises(PrivacyError):
        validate_projection({"content": "PRIVATE_SENTINEL"})


def test_projection_rejects_normalized_forbidden_keys_at_any_depth() -> None:
    with pytest.raises(PrivacyError):
        validate_projection({"execution": {"tool_Arguments": "PRIVATE_SENTINEL"}})


def test_projection_rejects_unknown_non_usage_metadata() -> None:
    with pytest.raises(PrivacyError):
        validate_projection({"metadata": {"note": "PRIVATE_SENTINEL"}})


def test_projection_drops_non_numeric_unknown_usage_values() -> None:
    assert validate_projection(
        {
            "usage": {
                "future_counter": 7,
                "future_flag": True,
                "description": "secret",
                "missing_value": None,
            }
        }
    ) == {"usage": {"future_counter": 7, "future_flag": True}}


def test_projection_keeps_numeric_reasoning_usage_without_allowing_reasoning_text() -> None:
    assert validate_projection({"usage": {"reasoning": 7}}) == {
        "usage": {"reasoning": 7}
    }
    with pytest.raises(PrivacyError):
        validate_projection(
            {"usage": {"reasoning": "PRIVATE_REASONING_SENTINEL"}}
        )


def test_projection_json_is_compact_and_sorted() -> None:
    assert projection_json({"usage": {"b": 1, "a": {"z": True}}}) == (
        '{"usage":{"a":{"z":true},"b":1}}'
    )
