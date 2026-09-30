# 036 — Opt-in clause/step checkpoints

Status: Approved for implementation after independent plan review and Lead QC.
Baseline: db184390e7b23e052c68ef3b04022dc5befec9c1.
Scope authorized by the user's instruction to start in E:; this spec narrows that authorization to the existing CLI review flow. No TUI, service, dependency, or reviewer-policy changes.

## User behavior

- `openreview precheck review INPUT --resume` writes/reuses successful extraction and QA steps for the exact same file and effective configuration. Parsing and privacy processing still run on every invocation.
- `--resume --force-review` removes only that identity's steps; the existing run and cost session remain. `--force-review` without `--resume` is a parameter error.
- `openreview precheck checkpoints-clear INPUT` deletes only new checkpoint runs/steps matching the current file bytes hash. Old file versions, PII mappings, reports, and cost logs remain. This is logical deletion, not secure erasure; later review creates a new cost session.
- Default non-resume behavior and TUI remain compatible. Scope is same-file review, one foreground process, no cross-version reuse or exactly-once claim.

## Storage and privacy

Add migration 016 with `review_checkpoint_runs` (identity digest, document hash, schema version, cost session, active/incomplete/completed, timestamps) and `review_checkpoint_steps` (run, clause id/hash, category, extraction/qa, running/completed/failed, encrypted payload, timestamps). Run deletion cascades only into the new steps table. Transactions cover complete step writes, not external requests.

Payloads use a strict explicit JSON whitelist and Fernet authenticated encryption. They contain assessment result fields, not clause_text, paths, request messages, credentials, raw responses, or exception strings. The current parsed/stripped text is injected only in memory when decoding. Reject unexpected fields, invalid enum/type, bool-as-number, nonfinite/out-of-range numbers, and wrong clause/category/slot identity. Bad individual ciphertext/schema is a safe cache miss; invalid key, unavailable database, or failing writes stop resume with a fixed safe error. Extraction is serialized before QA mutates an independent assessment object.

A new random key lives only in the existing user's AppData config directory (`review-checkpoints.key`), never in the worktree. Exclusive creation handles first-use races; existing invalid keys are not overwritten. No claim of Windows ACL enforcement, protection against same-user/admin access, or complete upstream data-at-rest security. Logs in strict extraction/QA and assessment.error use only fixed error categories, never str/repr(exc), provider errors, raw responses, or contract text.

## Identity and invalidation

Canonical identity uses document SHA-256, normalized absolute path digest, complete playbook dataclass + version, mode/thresholds/privacy/grounding settings, effective gateway configuration, checkpoint schema version, and a fixed startup manifest of relevant implementation files and installed parser/privacy/gateway dependency versions.

Bind the actual extraction and QA system prompts resolved through PromptStore with their runtime variable substitution using keyed HMAC. Do not hash the entire database: it contains unrelated mutable rows and checkpoints themselves.

Conservatively bind primary/fallback/recovery providers, their effective model mapping, endpoint, local attributes, capability/parameters/fallback/cost policies, resolved credential kwargs and endpoint-relevant environment using keyed HMAC. No credentials or unkeyed credential digest are stored. Model fields in stored assessments are slot labels, not claims about the provider/model actually executed after fallback. Remote alias version changes cannot be detected.

Before external calls, and again before successful persistence, verify that current file bytes, effective runtime config, playbook, and resolved prompts still match the run. Runtime change produces a fixed checkpoint failure and no further checkpoint writes. Implementation/dependency manifest is captured once at startup, not a general hot-reload framework.

## Failures, recovery, and costs

Only completed, authenticated, validated steps are reusable. Running/failed, malformed JSON, invalid required fields/enums and error assessments are misses. Strict validation is opt-in for resume; valid uncertain judgments are cacheable. No-match clauses require no model call and skip QA. Failed QA preserves extraction. Incomplete runs retain successful steps for next invocation.

Checkpoint safety errors must bypass Pipeline's normal stage retries and review runner exception/report conversion, reach CLI as a fixed error and nonzero exit, and halt multi-file processing. KeyboardInterrupt/SystemExit/cancellation are not swallowed. A committed result survives later interruptions; interruption after a remote reply but before commit can cause a repeated request and charge.

Reuse the run's cost session in extraction, QA, and grounding. Hits do not log hypothetical costs. Force preserves session/budget, and explicit mismatched session is rejected. Grounding, colors, reports and export rerun; zero model calls in benchmark means extraction+QA with grounding disabled, not complete pipeline zero-call behavior.

## TDD and acceptance

Each implementation unit records actual RED before production code and GREEN after. No changes to pytest/ruff/mypy configuration; no new dependencies.

1. Strict serialization/key: encrypted roundtrip, no plaintext clause/PII/key, malformed schema/key/ciphertext.
2. Store: fresh/upgrade/idempotent migration, transaction rollback, clear current bytes only and preserve unrelated data/session on force.
3. Identity: change resolved prompt, provider/endpoint/auth kwargs, privacy, playbook, file/path, fixed manifest => miss; unrelated DB updates => identity unchanged.
4. Strict extraction/QA: malformed JSON/invalid fields => fixed safe error; default contracts unchanged; no raw exception in log/report.
5. ReviewStage: per-step resume, failed QA only retries QA, independent extraction snapshot, no-match and cache hits avoid calls.
6. Runner/CLI: opt-in only; errors bypass stage retry/runner; exit nonzero and halt later files; no new key/cache in normal flow.
7. Chaos/privacy: interruption before request, after remote response before commit, corrupted payload, safe failure and plaintext scan.
8. Deterministic benchmark counts the actual extraction/QA gateway seam: baseline 3+3 calls, pre-request interruption after 2 extraction+1 QA then resume 1+2, full hit 0, force 6 same session, after-reply interruption repeats that step. Compare normalized results. Publish actual mocked counts/timing, not real accuracy or dollars.

## Deliverables and limits

New review/checkpoints.py and storage/checkpoints.py; migration; narrowly wired extraction/QA/ReviewStage/runner/CLI; focused unit/integration/chaos/privacy tests; benchmark script and checkpoint docs; README fork section. DeepSeek setup is a separate worker's scope. Verification record updated 2026-10-01: the strict-template fix passed independent review and 78 new tests passed on E:; the real DeepSeek public-NDA smoke and same-process resume repeat passed (2 then 0 SDK dispatches, no failed assessments, equal normalized results), recorded in docs/benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json. A new Python process restored equal results under an SDK request guard with 0 attempts, recorded in docs/benchmarks/checkpoints/clause-checkpoints-fresh-process.json. This is one candidate among five parsed clauses with grounding disabled, not accuracy, billing, general latency or production validation; hook results are recorded per commit, and the pytest hook collects tests only. Initial supported demo is text-readable English NDA; no OCR, cross-document reuse, distributed locks, web UI, legal correctness certification, or real-cloud cost claims.
