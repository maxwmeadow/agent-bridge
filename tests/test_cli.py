from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.cli import main


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_send_inbox_read_reply_flow(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "send", "--from", "claude", "--to", "codex", "--subject", "Test", "--body", "Hi"
    )
    assert code == 0
    message_id = out.split("Sent ")[1].split(" ")[0]

    code, out, _ = run(capsys, "inbox", "codex")
    assert code == 0
    assert "1 unread message for codex" in out
    assert message_id in out

    code, out, _ = run(capsys, "read", message_id, "--mark-read", "codex")
    assert code == 0
    assert "--- body ---\nHi" in out

    code, out, _ = run(capsys, "inbox", "codex")
    assert "No unread messages for codex" in out

    code, out, _ = run(capsys, "reply", message_id, "--from", "codex", "--body", "Ack")
    assert code == 0
    assert "to claude" in out

    code, out, _ = run(capsys, "threads")
    assert "1 recent thread" in out


def test_body_from_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Commit: 8c82f8f\nTests passed.\n", encoding="utf-8")

    code, _, _ = run(
        capsys,
        "send",
        "--from", "codex",
        "--to", "claude",
        "--subject", "Ready",
        "--body-file", str(body_file),
    )
    assert code == 0

    code, out, _ = run(capsys, "inbox", "claude")
    assert "Commit: 8c82f8f" in out


def test_errors_exit_nonzero_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(
        capsys, "send", "--from", "claude", "--to", "gpt", "--subject", "s", "--body", "b"
    )
    assert code == 1
    assert "Unknown agent 'gpt'" in err


def test_status_and_paths(capsys: pytest.CaptureFixture[str], db_path: Path) -> None:
    code, out, _ = run(capsys, "status", "--agent", "claude")
    assert code == 0
    assert "known agents:   claude, codex" in out

    code, out, _ = run(capsys, "paths")
    assert str(db_path) in out

    code, out, _ = run(capsys, "agents")
    assert out.split() == ["claude", "codex"]


def test_status_for_one_agent_and_set_status(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "status", "codex")
    assert code == 0
    assert "codex: unknown" in out

    code, out, _ = run(
        capsys,
        "set-status", "codex", "usage_exhausted",
        "--reason", "5-hour limit",
        "--resume-after", "2026-08-08T12:00:00Z",
    )
    assert code == 0
    assert "codex: usage_exhausted" in out
    assert "reported by:   cli" in out
    assert "5-hour limit" in out

    code, out, _ = run(capsys, "status")
    assert "usage_exhausted" in out
    assert "Status is what each agent reported" in out


def test_set_status_rejects_unknown_value(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):  # argparse choices
        run(capsys, "set-status", "codex", "on_fire")


def test_wait_times_out_with_exit_code_three(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "wait", "claude", "--timeout", "1")
    assert code == 3
    assert "timeout" in out


def test_wait_returns_immediately_for_existing_mail(capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "send", "--from", "codex", "--to", "claude", "--subject", "hi", "--body", "b")
    code, out, _ = run(capsys, "wait", "claude", "--timeout", "30")
    assert code == 0
    assert "message_received" in out
    assert "hi" in out


def test_cancel_wait(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "cancel-wait", "claude", "--reason", "manual")
    assert code == 0
    assert "sequence 1" in out
