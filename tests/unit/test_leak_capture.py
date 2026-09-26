"""Unit tests for tests.helpers.leak_capture."""

import sqlite3
from pathlib import Path

import pytest

from tests.helpers.leak_capture import Captured, assert_detects, capture


def test_assert_detects_finds_planted_canary() -> None:
    captured = Captured(stdout="before CANARY-123 after\n")

    # find_all is filtered to nonzero surfaces only.
    assert captured.find_all("CANARY-123") == {"stdout": 1}

    # Positive control: the canary is detectable.
    assert_detects(captured, "CANARY-123")

    # A needle that IS present must make assert_absent raise.
    with pytest.raises(AssertionError):
        captured.assert_absent("CANARY-123")


def test_assert_absent_raises_naming_every_surface() -> None:
    captured = Captured(
        stdout="clean\n",
        stderr="leak SECRET\n",
        logs={"/tmp/app.log": "SECRET twice SECRET\n"},
    )
    with pytest.raises(AssertionError) as excinfo:
        captured.assert_absent("SECRET")
    message = str(excinfo.value)
    assert "stderr" in message
    assert "/tmp/app.log" in message


def test_assert_absent_passes_when_needle_is_absent() -> None:
    captured = Captured(stdout="nothing to see\n", stderr="")
    captured.assert_absent("CANARY-123")  # must not raise


def test_capture_round_trips_stdout_logs_and_sqlite(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "run.log"
    log_file.write_text("log line CANARY-123\n", encoding="utf-8")

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE notes (name TEXT, count INTEGER)")
    conn.execute("INSERT INTO notes (name, count) VALUES (?, ?)", ("CANARY-123", 7))
    conn.execute("INSERT INTO notes (name, count) VALUES (?, ?)", ("alice", 1))
    conn.commit()
    conn.close()

    with capture(log_dir=log_dir, db_path=db_path) as cap:
        print("hello CANARY-123")

    assert "hello CANARY-123" in cap.stdout
    assert cap.logs[str(log_file)] == "log line CANARY-123\n"
    assert cap.db_text["notes.name"] == ["CANARY-123", "alice"]
    assert "notes.count" not in cap.db_text  # INTEGER affinity is not a text surface
    assert cap.find_all("CANARY-123") == {
        "stdout": 1,
        str(log_file): 1,
        "notes.name": 1,
    }


def test_add_cli_result_folds_stdout_and_stderr() -> None:
    class _Result:
        stdout = "out CANARY-123\n"
        stderr = "err\n"

    cap = Captured()
    cap.add_cli_result(_Result())
    assert cap.stdout == "out CANARY-123\n"
    assert cap.stderr == "err\n"
    assert cap.find_all("CANARY-123") == {"stdout": 1}


def test_assert_detects_raises_when_canary_is_missing() -> None:
    cap = Captured(stdout="nothing\n")
    with pytest.raises(AssertionError):
        assert_detects(cap, "CANARY-123")
