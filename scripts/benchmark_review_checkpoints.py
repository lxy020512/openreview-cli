"""Deterministic fault experiment; no cloud calls, credentials, or quality claims."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from openreview_cli.review.checkpoints import CheckpointSession
from openreview_cli.review.pipeline import ReviewStage
from openreview_cli.review.playbook import load_bundled


class TrialInterrupted(BaseException):
    """Simulated process interruption; ordinary model-error handlers cannot swallow it."""


def _normalized(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for assessment in result["review_assessments"]:
        rows.append(
            {
                key: value
                for key, value in asdict(assessment).items()
                if key not in ("color", "amber_reasons", "effective_confidence")
            }
        )
    return rows


def run_benchmark(work_dir: Path) -> dict[str, Any]:
    """Count dispatches at extraction/QA call_gateway_chat; input is synthetic."""
    if work_dir.exists() and any(work_dir.iterdir()):
        raise ValueError("benchmark work directory must be empty")
    work_dir.mkdir(parents=True, exist_ok=True)
    playbook = load_bundled()
    category = playbook.categories[0]
    document = work_dir / "synthetic-nda.docx"
    document.write_bytes(b"fixed synthetic document; not a parse/privacy benchmark")
    clauses = [
        SimpleNamespace(id=f"c{index}", text=f"{category.name} synthetic clause {index}")
        for index in (1, 2, 3)
    ]
    context = {"clauses": clauses, "document": SimpleNamespace(source_path=document, page_count=1)}
    counts = {"extraction": 0, "qa": 0}
    timeout_on_second_qa = False
    extraction_reply = json.dumps(
        {
            "position": "acceptable",
            "confidence": 0.8,
            "citation": "synthetic evidence",
            "category_match": True,
        }
    )
    qa_reply = json.dumps(
        {
            "verdict": "agree",
            "revised_position": None,
            "rationale": "",
            "citation_valid": True,
            "position_valid": True,
            "category_valid": True,
            "confidence_valid": True,
        }
    )

    def model_dispatch(slot: str, *args: Any, **kwargs: Any) -> str:
        nonlocal timeout_on_second_qa
        step = "extraction" if slot == "extraction" else "qa"
        counts[step] += 1
        if step == "qa" and timeout_on_second_qa and counts[step] == 2:
            timeout_on_second_qa = False
            raise TimeoutError("synthetic timeout")
        return extraction_reply if step == "extraction" else qa_reply

    def session(name: str, force: bool = False) -> CheckpointSession:
        return CheckpointSession(
            document,
            settings=lambda: {"playbook": asdict(playbook)},
            slots=("extraction", "reasoning"),
            db_path=work_dir / f"{name}.sqlite",
            key_path=work_dir / "isolated-test-config" / "key",
            force=force,
        )

    def run(checkpoints: CheckpointSession | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        counts.update(extraction=0, qa=0)
        start = time.perf_counter()
        stage = ReviewStage(playbook, qa_model="reasoning", checkpoints=checkpoints)
        result = asyncio.run(stage.run(context))
        assert result is not None
        return result, {"calls": counts.copy(), "elapsed_seconds": time.perf_counter() - start}

    receipt: dict[str, Any] = {
        "kind": "offline-mocked-gateway-fault-experiment",
        "schema_version": 1,
        "scope": "ReviewStage extraction+QA only; parsing/PII/grounding/cloud costs excluded",
    }
    with (
        patch("openreview_cli.review.checkpoints.runtime_snapshot", return_value={"mock": "fixed"}),
        patch("openreview_cli.review.extraction.call_gateway_chat", side_effect=model_dispatch),
        patch("openreview_cli.review.qa.call_gateway_chat", side_effect=model_dispatch),
    ):
        baseline, receipt["baseline"] = run(session("baseline"))
        receipt["baseline"]["description"] = "Cold, uninterrupted opt-in review reference"
        checkpoints = session("before-request")
        original_begin = checkpoints.begin

        def interrupt_before_request(clause: Any, step: str, category_id: str) -> None:
            if clause.id == "c2" and step == "qa":
                raise TrialInterrupted()
            original_begin(clause, step, category_id)

        counts.update(extraction=0, qa=0)
        try:
            with patch.object(checkpoints, "begin", side_effect=interrupt_before_request):
                asyncio.run(
                    ReviewStage(playbook, qa_model="reasoning", checkpoints=checkpoints).run(
                        context
                    )
                )
        except TrialInterrupted:
            receipt["interrupted_before_request"] = {"calls": counts.copy()}
        resumed, receipt["resumed"] = run(session("before-request"))
        receipt["resumed"]["results_equal_baseline"] = _normalized(resumed) == _normalized(baseline)
        _, receipt["full_hit"] = run(session("before-request"))
        forced = session("before-request", force=True)
        _, receipt["force"] = run(forced)
        receipt["force"]["same_session"] = forced.run.session_id == checkpoints.run.session_id

        after_reply = session("after-reply")
        original_save = after_reply.save

        def interrupt_before_commit(clause: Any, step: str, assessment: Any) -> None:
            if clause.id == "c2" and step == "extraction":
                raise TrialInterrupted()
            original_save(clause, step, assessment)

        counts.update(extraction=0, qa=0)
        try:
            with patch.object(after_reply, "save", side_effect=interrupt_before_commit):
                asyncio.run(
                    ReviewStage(playbook, qa_model="reasoning", checkpoints=after_reply).run(
                        context
                    )
                )
        except TrialInterrupted:
            interrupted_calls = counts.copy()
        recovered, details = run(session("after-reply"))
        receipt["after_reply_before_commit"] = {
            "interrupted_calls": interrupted_calls,
            "resumed_calls": details["calls"],
            "total_calls": sum(interrupted_calls.values()) + sum(details["calls"].values()),
            "results_equal_baseline": _normalized(recovered) == _normalized(baseline),
            "limit": "A completed external reply before SQLite commit may be requested again.",
        }
        timeout_on_second_qa = True
        failed, receipt["qa_timeout"] = run(session("qa-timeout"))
        receipt["qa_timeout"]["failed_assessments"] = sum(
            a.error is not None for a in failed["review_assessments"]
        )
        recovered, receipt["qa_timeout_resume"] = run(session("qa-timeout"))
        receipt["qa_timeout_resume"]["results_equal_baseline"] = _normalized(
            recovered
        ) == _normalized(baseline)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--work-dir", type=Path, help="Optional isolated synthetic experiment directory."
    )
    args = parser.parse_args()
    if args.work_dir:
        try:
            receipt = run_benchmark(args.work_dir)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        with tempfile.TemporaryDirectory(prefix="openreview-checkpoints-") as directory:
            receipt = run_benchmark(Path(directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote offline experiment receipt: {args.output}")


if __name__ == "__main__":
    main()
