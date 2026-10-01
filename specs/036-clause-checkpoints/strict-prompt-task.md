# Strict extraction prompt contract

Status: Approved for implementation after independent plan review.

## Problem and approved change

The common extraction prompt advertises `no-match`, while the opt-in strict
validator and checkpoint codec accept the `Position` enum: `preferred`,
`acceptable`, `walkaway`, and `uncertain`. A correctly typed JSON response can
therefore be rejected because the prompt requests a value outside that schema.

Add an optional `strict=False` argument to the common prompt builder and pass
the extraction caller's strict flag. In strict mode, derive advertised position
values from `Position`. Instruct category mismatch or insufficient evidence to
produce `position=uncertain` and `category_match=false`, without suggesting a
category default in that case. Preserve the default template and non-resume
behavior. Keep strict rejection of `no-match` and unknown values.

No validator relaxation, default-position conversion, YAML binding change,
gateway/auth expansion, dependency, configuration, or QA change is authorized.
The QA prompt's advertised verdicts and revised positions are already accepted
by its strict validator, so no QA schema fix is needed.

## Acceptance and TDD record

- Every mode's strict prompt advertises exactly the `Position` values, all of
  which pass strict response validation.
- The actual extraction message path follows the caller's strict flag.
- A typed `uncertain` response with `category_match=false` remains uncertain,
  error-free, and round-trips through `CheckpointCodec`.
- Strict `no-match`, unknown position, malformed JSON, and bool-as-confidence
  responses retain the fixed `extraction_invalid_response` error.
- The default prompt retains `no-match` and its category-default guidance.
- An isolated `PromptStore` loaded with defaults resolves empty for unbound
  slots; explicit binding activates only that slot.

Actual RED, before production changes (2026-09-30):

```powershell
uv run --no-sync pytest tests/unit/test_strict_prompt_contract.py tests/unit/test_checkpoint_strict.py -q --allow-hosts=127.0.0.1,::1 --basetemp 'C:\Users\admin\Documents\ChatGPT\ai agent\.test-runs\strict-prompt-red-20260930-1'
```

Result: **27 failed, 9 passed in 1.26s**. The new strict builder argument was
missing, and the actual strict extraction message still advertised `no-match`
instead of `uncertain`. Existing rejection tests, the new `no-match` rejection,
the uncertain codec roundtrip, and isolated unbound defaults already passed.

Actual GREEN, after the minimal builder/caller change:

```powershell
uv run --no-sync pytest tests/unit/test_strict_prompt_contract.py tests/unit/test_checkpoint_strict.py -q --allow-hosts=127.0.0.1,::1 --basetemp 'C:\Users\admin\Documents\ChatGPT\ai agent\.test-runs\strict-prompt-green-20260930-1'
```

Result: **36 passed in 0.96s**.

Regression after formatting:

```powershell
uv run --no-sync pytest tests/unit/test_strict_prompt_contract.py tests/unit/test_checkpoint_strict.py tests/unit/test_prompts.py tests/unit/test_extraction_agent.py tests/unit/test_qa_agent.py tests/unit/test_prompt_defaults.py tests/unit/test_prompt_store.py tests/unit/test_checkpoint_prompt_store.py tests/unit/test_checkpoint_identity.py tests/unit/test_review_stage_checkpoints.py tests/unit/test_review_checkpoints.py tests/integration/test_review_resume_cli.py tests/redteam/test_checkpoint_pii_at_rest.py -q --allow-hosts=127.0.0.1,::1 --basetemp 'C:\Users\admin\Documents\ChatGPT\ai agent\.test-runs\strict-prompt-regression-20260930-1'
```

Result: **117 passed in 6.97s**. Scoped Ruff lint/format checks and strict mypy
passed for the two source and two test files; `git diff --check` passed.
A direct comparison against the baseline `HEAD` builder confirmed identical
default extraction messages for all 24 modes.

All runs used `E:\Projects\.tools\uv\uv.exe` with `--no-sync` against the existing
`C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime`, workspace C:
cache and source paths, `PYTHONUTF8=1`, `LITELLM_LOCAL_MODEL_COST_MAP=True`,
`HF_HUB_OFFLINE=1`, and `TRANSFORMERS_OFFLINE=1`. No credentials, real provider
requests, configuration/dependency changes, E: writes, commits, or pushes were
performed. These deterministic checks establish prompt/schema compatibility,
not real-provider accuracy or a guaranteed response format.
