from pathlib import Path

import pytest

from scripts.benchmark_review_checkpoints import run_benchmark


def test_benchmark_counts_real_review_gateway_seam_and_window(tmp_path: Path) -> None:
    receipt = run_benchmark(tmp_path)
    assert receipt["baseline"]["calls"] == {"extraction": 3, "qa": 3}
    assert receipt["interrupted_before_request"]["calls"] == {"extraction": 2, "qa": 1}
    assert receipt["resumed"]["calls"] == {"extraction": 1, "qa": 2}
    assert receipt["full_hit"]["calls"] == {"extraction": 0, "qa": 0}
    assert receipt["force"]["calls"] == {"extraction": 3, "qa": 3}
    assert receipt["force"]["same_session"] is True
    assert receipt["after_reply_before_commit"]["total_calls"] == 7
    assert receipt["resumed"]["results_equal_baseline"] is True
    assert receipt["qa_timeout_resume"]["calls"] == {"extraction": 0, "qa": 1}


def test_benchmark_refuses_existing_trial_data(tmp_path: Path) -> None:

    run_benchmark(tmp_path)
    with pytest.raises(ValueError, match="benchmark work directory must be empty"):
        run_benchmark(tmp_path)
