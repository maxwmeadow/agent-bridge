"""Availability, failure, and usage are three separate things.

The rule these tests defend: a high usage percentage is a metric, never a
reason to call an agent unavailable, and different failures stay
distinguishable instead of collapsing into ``usage_exhausted``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.codex_usage import read_latest_usage
from agent_bridge.config import CLAUDE_STOP_FAILURE_TYPES
from agent_bridge.errors import ValidationError
from agent_bridge.hooks import main as hook_main
from agent_bridge.store import MessageStore


# ------------------------------------------------------ failure projection


@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        ("rate_limit", "usage_exhausted"),
        ("billing_error", "usage_exhausted"),
        ("authentication_failed", "auth_error"),
        ("oauth_org_not_allowed", "auth_error"),
        ("overloaded", "unresponsive"),
        ("server_error", "unresponsive"),
    ],
)
def test_failures_project_onto_availability(
    store: MessageStore, error_type: str, expected_status: str
) -> None:
    record = store.record_failure("codex", error_type, detail="from the client")
    assert record.status == expected_status
    # The raw kind survives the projection, so the distinction is not lost.
    assert record.last_failure_kind == error_type
    assert record.last_failure_at is not None


@pytest.mark.parametrize(
    "error_type", ["invalid_request", "model_not_found", "max_output_tokens", "unknown"]
)
def test_request_level_failures_do_not_change_availability(
    store: MessageStore, error_type: str
) -> None:
    store.set_status("codex", "available")
    record = store.record_failure("codex", error_type, detail="one bad request")

    assert record.status == "available"  # still fine for the next thing
    assert record.last_failure_kind == error_type  # but the failure is on record


def test_provider_trouble_is_never_usage_exhaustion(store: MessageStore) -> None:
    """The distinction the whole failure vocabulary exists to preserve."""
    for error_type in ("overloaded", "server_error"):
        record = store.record_failure("codex", error_type)
        assert record.status != "usage_exhausted"
        assert record.last_failure_kind == error_type


def test_every_documented_error_type_is_accepted(store: MessageStore) -> None:
    for error_type in CLAUDE_STOP_FAILURE_TYPES:
        assert store.record_failure("codex", error_type).last_failure_kind == error_type


def test_unknown_failure_kind_is_rejected(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="Unknown failure kind"):
        store.record_failure("codex", "vibes")


def test_rate_limit_adopts_a_known_future_reset_as_resume_after(store: MessageStore) -> None:
    store.record_usage(
        "codex",
        percent=99.0,
        window="five_hour",
        resets_at="2099-01-01T00:00:00Z",
        source="claude_statusline",
    )
    record = store.record_failure("codex", "rate_limit")
    assert record.resume_after is not None
    assert record.resume_after.startswith("2099-01-01")


def test_a_past_reset_is_not_used_as_resume_after(store: MessageStore) -> None:
    store.record_usage(
        "codex",
        percent=99.0,
        window="five_hour",
        resets_at="2020-01-01T00:00:00Z",
        source="claude_statusline",
    )
    record = store.record_failure("codex", "rate_limit")
    assert record.resume_after is None  # a stale sample must not invent one


# ------------------------------------------------------------------ usage


def test_usage_is_a_metric_not_an_availability_signal(store: MessageStore) -> None:
    store.set_status("codex", "available")
    record = store.record_usage(
        "codex", percent=99.9, window="five_hour", resets_at=None, source="claude_statusline"
    )

    assert record.usage is not None
    assert record.usage.percent == pytest.approx(99.9)
    # The whole point: nearly out of quota is still available.
    assert record.status == "available"
    assert not record.is_unavailable


def test_usage_records_provenance(store: MessageStore) -> None:
    official = store.record_usage(
        "claude", percent=10.0, window="five_hour", resets_at=None, source="claude_statusline"
    )
    assert official.usage is not None and official.usage.is_official_source

    best_effort = store.record_usage(
        "codex", percent=10.0, window="primary", resets_at=None, source="codex_rollout"
    )
    assert best_effort.usage is not None and not best_effort.usage.is_official_source


def test_usage_input_is_validated(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="between 0 and 100"):
        store.record_usage(
            "codex", percent=150.0, window="five_hour", resets_at=None, source="manual"
        )
    with pytest.raises(ValidationError, match="Unknown usage window"):
        store.record_usage("codex", percent=1.0, window="fortnight", resets_at=None, source="manual")
    with pytest.raises(ValidationError, match="Unknown usage source"):
        store.record_usage("codex", percent=1.0, window="five_hour", resets_at=None, source="a hunch")


# ------------------------------------------------------------------- hook


def run_hook(payload: dict[str, object], monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(payload)))
    return hook_main(["--agent", "claude", *args])


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def test_stop_failure_hook_records_rate_limit(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = run_hook(
        {
            "session_id": "abc123",
            "hook_event_name": "StopFailure",
            "cwd": "/tmp",
            "error_type": "rate_limit",
            "error_message": "Rate limit exceeded",
        },
        monkeypatch,
    )
    assert code == 0

    record = store.get_status("claude")
    assert record.status == "usage_exhausted"
    assert record.last_failure_kind == "rate_limit"
    assert record.last_failure_detail == "Rate limit exceeded"


def test_stop_failure_hook_keeps_provider_errors_distinct(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_hook(
        {"hook_event_name": "StopFailure", "error_type": "overloaded", "error_message": "busy"},
        monkeypatch,
    )
    record = store.get_status("claude")
    assert record.last_failure_kind == "overloaded"
    assert record.status == "unresponsive"
    assert record.status != "usage_exhausted"


def test_stop_failure_hook_does_not_invent_undocumented_values(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_hook(
        {"hook_event_name": "StopFailure", "error_type": "quota_gone", "error_message": "?"},
        monkeypatch,
    )
    record = store.get_status("claude")
    assert record.last_failure_kind == "unknown"
    assert "quota_gone" in (record.last_failure_detail or "")
    # An unrecognised value must not be guessed into an availability change.
    assert record.status == "unknown"


def test_session_end_hook_uses_documented_end_reason(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_hook({"hook_event_name": "SessionEnd", "end_reason": "logout"}, monkeypatch)
    assert store.get_status("claude").status == "auth_error"

    run_hook({"hook_event_name": "SessionEnd", "end_reason": "prompt_input_exit"}, monkeypatch)
    assert store.get_status("claude").status == "client_closed"


def test_session_end_clear_is_not_an_availability_change(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set_status("claude", "available")
    run_hook({"hook_event_name": "SessionEnd", "end_reason": "clear"}, monkeypatch)
    assert store.get_status("claude").status == "available"


def test_statusline_hook_records_usage_and_reset(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = run_hook(
        {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 4102444800},
                "seven_day": {"used_percentage": 71.2, "resets_at": 4102531200},
            },
        },
        monkeypatch,
        "--statusline",
    )
    assert code == 0

    record = store.get_status("claude")
    assert record.usage is not None
    # The window closest to exhaustion is the one worth tracking.
    assert record.usage.window == "seven_day"
    assert record.usage.percent == pytest.approx(71.2)
    assert record.usage.resets_at is not None and record.usage.resets_at.startswith("2100-")
    # And it still says nothing about availability.
    assert record.status == "unknown"


def test_hook_never_fails_the_session(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every malformed input path must still exit 0."""
    monkeypatch.setattr("sys.stdin", _FakeStdin("not json at all"))
    assert hook_main(["--agent", "claude"]) == 0

    monkeypatch.setattr("sys.stdin", _FakeStdin("[]"))
    assert hook_main(["--agent", "claude"]) == 0

    monkeypatch.setattr("sys.stdin", _FakeStdin(""))
    assert hook_main(["--agent", "claude"]) == 0

    monkeypatch.setattr("sys.stdin", _FakeStdin('{"hook_event_name": "PreToolUse"}'))
    assert hook_main(["--agent", "claude"]) == 0


def test_hook_with_no_identity_still_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_BRIDGE_AGENT", raising=False)
    monkeypatch.setattr("sys.stdin", _FakeStdin('{"hook_event_name": "StopFailure"}'))
    assert hook_main([]) == 0


# ---------------------------------------------------- codex usage adapter


def write_rollout(root: Path, name: str, records: list[dict[str, object]]) -> Path:
    directory = root / "2026" / "08" / "08"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


RATE_LIMITS = {
    "limit_id": "codex",
    "primary": {"used_percent": 48.0, "window_minutes": 10080, "resets_at": 1786674570},
    "secondary": None,
    "plan_type": "plus",
    "rate_limit_reached_type": None,
}


def test_codex_adapter_reads_a_sample(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        "rollout-2026-08-08T02-41-13-abc.jsonl",
        [
            {"type": "event_msg", "payload": {"nothing": True}},
            {"type": "token_count", "rate_limits": RATE_LIMITS},
        ],
    )

    sample = read_latest_usage(tmp_path)
    assert sample is not None
    assert sample.percent == pytest.approx(48.0)
    assert sample.window == "primary"
    assert sample.plan_type == "plus"
    assert sample.limit_reached is False
    assert sample.resets_at is not None


def test_codex_adapter_finds_nested_payloads(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        "rollout-nested.jsonl",
        [{"type": "token_count", "payload": {"rate_limits": RATE_LIMITS}}],
    )
    sample = read_latest_usage(tmp_path)
    assert sample is not None and sample.percent == pytest.approx(48.0)


def test_codex_adapter_reports_a_reached_limit(tmp_path: Path) -> None:
    limits = dict(RATE_LIMITS, rate_limit_reached_type="primary")
    write_rollout(tmp_path, "rollout-hit.jsonl", [{"rate_limits": limits}])

    sample = read_latest_usage(tmp_path)
    assert sample is not None and sample.limit_reached is True


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"type": "token_count"}],
        [{"rate_limits": "not an object"}],
        [{"rate_limits": {"primary": {"used_percent": "lots"}}}],
        [{"rate_limits": {"primary": {"used_percent": 900}}}],
        [{"rate_limits": {}}],
    ],
)
def test_codex_adapter_returns_none_instead_of_raising(
    tmp_path: Path, records: list[dict[str, object]]
) -> None:
    write_rollout(tmp_path, "rollout-junk.jsonl", records or [{"x": 1}])
    assert read_latest_usage(tmp_path) is None


def test_codex_adapter_survives_truncated_json(tmp_path: Path) -> None:
    """A rollout file being written right now has a partial trailing line."""
    directory = tmp_path / "2026" / "08" / "08"
    directory.mkdir(parents=True)
    (directory / "rollout-partial.jsonl").write_text(
        json.dumps({"rate_limits": RATE_LIMITS}) + '\n{"rate_limits": {"primary": {"used_',
        encoding="utf-8",
    )
    sample = read_latest_usage(tmp_path)
    assert sample is not None and sample.percent == pytest.approx(48.0)


def test_codex_adapter_handles_a_missing_directory(tmp_path: Path) -> None:
    assert read_latest_usage(tmp_path / "does-not-exist") is None


def test_codex_adapter_is_not_wired_into_messaging(store: MessageStore, tmp_path: Path) -> None:
    """Messaging and waiting must not depend on the adapter at all."""
    assert read_latest_usage(tmp_path / "nope") is None
    sent = store.send(sender="codex", recipient="claude", subject="still works", body="yes")
    assert store.inbox("claude")[0].id == sent.id
