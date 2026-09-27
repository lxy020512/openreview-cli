# Gateway seam — task breakdown for #143 #144 #146 #147 #149 #150 #151 #152 #153 #154

**Deliverable of this doc:** 8 sequential TDD tasks that take the worktree from red to green, the
global verification order, a risk register, and — task-by-task, exhaustively and without overlap — the
test nodes each task owns.

**Authoritative input:** `docs/specs/plans/2026-09-27-fix-10-gateway-seam-design.md` (the Brainstorm
design doc, read-only). This plan implements that design **except** for the adjudications in §0.2 and
the refinements in §0.3, each backed by code evidence re-verified in this worktree.

**Base:** worktree `gateway-seam`. `git status --porcelain` on entry shows the two untracked plan
docs only (this file and the design doc). This plan adds no third file.

**Baseline re-measured, not assumed** (2026-09-27, this worktree). This is the **only** command in the
whole plan that uses `--runxfail`; it is re-run once more at the very end (§Global verification 1):

```
uv run pytest --runxfail -q -p no:randomly --no-header \
  tests/redteam/test_redteam_egress_guard.py::test_unclassifiable_provider_dispatch_increments_the_cloud_counter \
  tests/redteam/test_redteam_egress_guard.py::test_retry_dispatches_increment_the_counter_once_each \
  tests/redteam/test_redteam_egress_guard.py::test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier \
  tests/redteam/test_redteam_gateway_setup.py::test_gateway_test_does_not_echo_key_material_from_a_provider_error \
  tests/chaos/test_chaos_gateway_faults.py::test_no_choice_response_is_a_typed_error_and_logs_no_cost \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_dispatch_failure_is_a_typed_gateway_error \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_without_a_completion_marker_is_not_reported_as_done \
  tests/chaos/test_chaos_accounting.py::test_permanent_errors_are_not_retried
=> 12 failed   (8 node ids, 12 cases: 4 unparametrised + 4 parametrised x2)
```

---

## 0. Rules for the implementing agent

### 0.1 Ordering and command rules

1. **Do the tasks in order T1 → T8.** They share one seam (`Gateway._call_with_fallback`) and later
   tasks edit lines earlier tasks wrote.
2. **Per task, the first edit is a test edit.** Run the task's **Red** command; it must fail for the
   reason stated. Then make the minimal source edit; run the **Green** command; it must pass. Then run
   the task's **no-regression** command before moving on.
3. **`--runxfail` appears in exactly one command in this document** — the 8-node probe (baseline and
   final). Use **plain** `uv run pytest -q -p no:randomly …` everywhere else, with an **explicit
   expected count** (`2 passed`, `8 failed`, `8 failed, 4 passed`, …). Rationale: under `--runxfail`
   every still-marked, still-failing node reports `FAILED`, so "0 failed" is unreachable mid-plan.
   Without it, a still-marked node reports `xfailed` and is **not** a failure, so "0 failed" is a
   meaningful, reachable assertion at every point.
4. Never leave an `xfail` marker on a node whose expectation you have just implemented. Never delete
   a probe node; **invert** it in place and rename it, so the old behaviour stays recorded in the diff.
5. Do not reformat, ruffle-fix or "tidy" anything outside the files a task names. `ruff format`
   reformats whole files; run it only on files you edited.
6. `uv run ruff check . && uv run ruff format .` and `uv run mypy src/ tests/` must pass after every
   task (the pre-commit hook runs them). `mypy` is strict for `src/` and `tests/` except the modules
   listed under `[[tool.mypy.overrides]]` (`tests.integration.tui.*`, `tests.unit.tui.*`,
   `tests.exploratory.*`, …). Any new test double you add to a non-exempt test file needs real
   annotations. Ruff's `select` list does **not** include `ARG`, so an as-yet-unused parameter is fine.
7. Python is run with `uv run`. Use `-p no:randomly` on every single-task invocation so a failure is
   reproducible; the final full-suite run (Global verification 7) deliberately does **not** use it.

### 0.2 Lead adjudications folded in (and where they change the design doc)

| # | Adjudication | Effect on the design doc |
|---|---|---|
| A1 | **No new error classes.** A reply with no usable choice reuses `UnclassifiedProviderError` (the existing `_classify_error` catch-all) with the provider name **in the message**; a truncated stream reuses `gateway.errors.ConnectionError` (the class `_iter_stream` already raises for transport failures). | The design's `EmptyResponseError` and `TruncatedStreamError` (design §3.4, §3.6, §7.5) are **not** created. Every `EmptyResponseError` in this doc means `UnclassifiedProviderError`; every `TruncatedStreamError` means `gateway.errors.ConnectionError` with a truncation message. |
| A2 | **#144 stays minimal:** re-derive the provider block for the fallback model from the issue's own suggested fix. `_provider_kwargs_for` is **not** extracted; the fallback helpers are **collapsed into the fallback branch** (no new method names). | The design's `_switch_to_fallback` / `_retarget_provider_kwargs` (§3.2) are **inlined** into `_call_with_fallback`. The credential-kwarg drop + `api_base` re-derivation stay — see §0.3(D2), a concrete custom-provider defect, not a refactor. |
| A3 | **#146:** the regex `redact_text()` at `_classify_error` **plus** the `gateway test` echo are required. `register_secret()`, `_registered_secrets`, `_MIN_SECRET_LEN` and all three registration seams are **cut**; `_format_exception` is **not** touched. | The design's §3.3 `register_secret` machinery is **dropped**. `redact_text` = the `_KEY_VALUE_RE` pass + the literal `REDACT_PATTERNS` pass (with the `patterns` parameter kept, §0.3 D1). The residual gap is a documented limitation (§Documented changes). |
| A4 | **#151/#152 streaming:** `chat_stream` is routed through `_call_with_fallback` — and that routing, its `_record_cloud_call` (`router.py:674`) and the `_iter_stream` classification are owned **entirely by T5**, never by T1. | The design's `_iter_stream`-owns-`done`-and-cost-log sketch (§3.6) is replaced by §0.3(D3). |

### 0.3 The evidence-backed refinements to the design doc

**(D1) `redact_text(text, patterns=REDACT_PATTERNS)` — do not let `RedactingFilter._redact` lose its
per-instance patterns.**
The design (§3.3) says "`RedactingFilter._redact` becomes a call to `redact_text`". Taken literally
that makes `RedactingFilter(patterns)` ignore its argument and apply the module-level
`REDACT_PATTERNS`, and **two currently-green unit tests fail**:

* `tests/unit/test_gateway_redaction.py::TestRedactingFilter::test_redacts_matching_string_in_record`
  asserts `record.msg == "Using OPEN**********=sk-abc123"`; the module list's literal `"sk-"` pattern
  would additionally turn `=sk-abc123` into `=***abc123`.
* `...::test_redacts_multiple_patterns` builds `RedactingFilter(["sk-", "OPENAI"])` and asserts
  `"OPENAI" not in record.msg`; `REDACT_PATTERNS` contains `OPENAI_API_KEY`, not `OPENAI`, so the
  assertion fails.

Fix: `redact_text` takes one optional `patterns` argument defaulting to `REDACT_PATTERNS`, and
`RedactingFilter._redact` delegates with `self._patterns`. One implementation, both pinned tests keep
passing. **No secret-value registration** (A3): the literal pass plus the `_KEY_VALUE_RE` pass is the
whole function.

**(D2) The fallback dispatch must drop the primary's provider kwargs, not only its `api_base` — and
that leak is `source == "custom"`-scoped.**
`_get_litellm_kwargs` puts the primary's `api_base` (`router.py:401-402`), its declared
`CredentialField`s (`info.credentials[*].litellm_param`, `:403-405`) and — **only for a
`source == "custom"` provider with a `base_url`** — an explicit `api_key` into the same `call_kwargs`
dict that `_call_with_fallback` reuses for the fallback (`:410-418`). litellm honours an explicit
`api_key` over the provider env var, so "re-derive `api_base` only" would still send the **primary's
credential to the fallback provider's host**.

Bundled providers (`openai`, `anthropic`, …) declare **no** `credentials` and are not `custom`
(`src/openreview_cli/gateway/models.json`), so for them the pop-or-not is behaviourally inert — the
`api_base` re-derivation is the only visible half. The **TDD anchor must therefore drive a `custom`
primary** (§T2) so the pop is load-bearing and the anchor actually discriminates; without that the
anchor would pass on the unfixed code. The fallback branch pops `api_base`, `api_key` and the
primary's declared credential kwargs, then applies the fallback's `api_base` +
`_apply_provider_credentials` (an existing helper — no new abstraction). It deliberately does **not**
re-implement the custom-provider `openai/` rewrite for the fallback model; a custom provider as a
*fallback* is covered by no issue and no probe, and dropping a key fails closed with a 401 rather than
leaking a credential. Recorded in the risk register.

**(D3) `chat_stream` owns the cost row and the `done` event; `_iter_stream` stays a pure stream
transform.** The design (§3.6) puts both inside `_iter_stream`, which would force three new parameters
(`slot`, `session_id`, `model`) through a private method whose only caller is `chat_stream` — plumbing
cost context into a stream transformer, i.e. exactly the "one-off abstraction" the project rules
reject. Instead `_iter_stream` yields chunk events only and raises on truncation, and `chat_stream`
does `for event in self._iter_stream(...): yield event` → cost log (non-fatal) → `yield
StreamingOutputEvent(type="done")`. Same ordering contract (terminal marker → cost row → `done`), one
function owns the cost context, `_iter_stream`'s signature is unchanged. Verified safe: `_iter_stream`
is referenced only at `router.py:685` and `:687`.

**(D4) The terminal check reads the RESPONSE, not the chunk, and there is a single predicate.** The
check is `if not getattr(response, "received_finish_reason", None): raise ConnectionError(...)`.
Re-measured in this worktree against `tests/chaos/_w7_probe.LocalSSEServer`:

| server mode | streamed chunks `(delta.content, chunk.choices[0].finish_reason)` | `received_finish_reason` | `intermittent_finish_reason` |
|---|---|---|---|
| `healthy` | `('hi',None) ('!',None) (None,'stop')` | `'stop'` | `'stop'` |
| `mid_stream_disconnect` | `('hi',None) (None,'stop')` | `None` | `None` |
| `truncated_stream` | `(None,'stop')` | `None` | `None` |
| `malformed_body` | raises `litellm.MidStreamFallbackError` on the **first `next()`** | — | — |
| `provider_down` | raises `litellm.InternalServerError` (`status_code == 500`) from `completion()` itself | — | — |

`intermittent_finish_reason` never differs from `received_finish_reason` in any measured mode, so the
predicate reads **only** `received_finish_reason`. A per-chunk `finish_reason` predicate would call a
truncated stream complete (litellm fabricates `"stop"` on EOF), and both the dispatch-level fix (T5)
and the iteration-level fix (T6) are needed because the two failure shapes surface at different times.

### 0.4 STREAMING DECISION (#151/#152) — explicit answers to the lead's questions

* **Is the design's route (`chat_stream` → `_call_with_fallback`) workable?** Yes, with the
  `setdefault` fix the design itself identifies. `chat_stream` sets `call_kwargs["timeout"]` to the
  dual `httpx.Timeout(connect=15, read=45)` at `router.py:665-670` **before** dispatch, and
  `_call_with_fallback` overwrites `call_kwargs["timeout"]` with the int `60` at `:535`. Left
  unfixed, `test_stream_timeout_is_dual_not_single` and `test_stream_idle_timeout_cuts_stalled_provider`
  break and the FR-6 idle-timeout contract is silently destroyed. Fix: `call_kwargs.setdefault("timeout", timeout)`.
  `chat`/`embed`/`rerank` pass no `timeout`, so their behaviour is bit-identical.
* **Does streaming get retries + fallback, or stay one-shot?** **Retries and fallback apply to the
  DISPATCH only.** At dispatch time nothing has been yielded yet, so re-dispatching cannot duplicate
  or interleave output (design C3); `provider_down` fails *eagerly* inside `completion(stream=True)`
  (§0.3 D4), so the retry loop genuinely helps there. After the first chunk is yielded the gateway
  never re-dispatches: a mid-stream failure is *classified and raised*. `num_retries = 0` stays —
  litellm must not retry underneath the gateway's own layer.
* **Ownership (blocking finding 1):** the *whole* stream-policy change — routing `chat_stream` through
  `_call_with_fallback`, deleting `chat_stream`'s post-dispatch counter at `router.py:674`, the
  `setdefault` timeout fix, and the `_iter_stream` classification — belongs to **T5**. **T1 must not
  touch `chat_stream` at all** (see T1's Files and test edits). T6 owns the terminal marker + stream
  cost row.
* **What does `test_chat_stream_disables_retries_and_never_tries_the_fallback` become?**
  `test_chat_stream_retries_then_tries_the_fallback` (T5): `flaky.seam.attempts == 4`,
  `flaky.seam.models == ["openai/gpt-4o"] * 3 + ["anthropic/claude-3-5-haiku"]`,
  `isinstance(outcome.caught, GatewayError)` (drop the `type(...).__name__ == "ConnectionError"` and
  `not isinstance` lines), `flaky.gw._cloud_calls_made == 4`, `cost_rows == []` (unchanged).
* **Honest consequence of A1 for streams:** a truncation now raises `gateway.errors.ConnectionError`,
  which the recovery layer maps to HTTP 503 → transient → `provider_fallback`. `chat_stream` is
  consumed nowhere in `src/` (grep: only `router.py`), so no recovery path sees it today; the risk
  register records it.

### 0.5 Anchor tests that must stay green with **no edit** (anti-regression list)

`tests/unit/test_gateway_router.py::TestChat::test_falls_back_to_fallback_model` · `::TestRerank::*`
(incl. `test_rerank_error_names_provider`, `test_rerank_applies_provider_credentials`) ·
`::TestCustomProviderRouting::*` · `::test_stream_idle_timeout_cuts_stalled_provider` ·
`tests/unit/test_gateway_tier_enforcement.py` strict-tier block tests ·
`tests/unit/test_gateway_redaction.py::TestRedactingFilter::*` (both nodes cited in D1) ·
`tests/unit/test_log_redaction_handlers.py::*` ·
`tests/unit/test_api_key_redaction_e2e.py::test_sk_key_redacted_in_stdout_stderr_and_logfile` ·
`tests/redteam/test_redteam_gateway_setup.py::test_gateway_test_never_logs_the_key_even_at_debug_level`
(its `"***t"` reachability assertion must survive T3) ·
`tests/redteam/test_redteam_egress_guard.py::test_counter_equality_per_dispatch_site` (needs the T6
harness double, no assertion change) ·
`tests/chaos/test_chaos_gateway_faults.py::test_healthy_local_stream_completes_with_chunks_and_done` ·
all `test_negative_control_*` oracles · `tests/integration/test_gateway_streaming.py::*` ·
`tests/integration/tui/test_status_bar_egress.py::*`, `test_explore_egress_modal.py::*`.

---

## T1 — Count every dispatch, and fail closed on an unclassifiable provider

**Goal.** `_cloud_calls_made` / `get_total_cloud_calls()` increment **immediately before each
`call_fn(**call_kwargs)` that reaches the provider SDK**, on every attempt of the retry loop and on
the fallback dispatch, and an unclassifiable provider resolves as **cloud** exactly like
`_enforce_tier`.

**Issues.** #143, #147 (one fix, one arithmetic — they cannot be separated: the #143 probe asserts
`== len(recorder.records)`, so the fail-closed flip alone leaves it red).

**Files.** `src/openreview_cli/gateway/router.py`;
`tests/redteam/test_redteam_egress_guard.py`; `tests/chaos/test_chaos_gateway_faults.py`;
`tests/chaos/test_chaos_accounting.py`; `tests/exploratory/test_explore_egress_counter.py`;
`tests/unit/test_gateway_router.py`; `tests/unit/test_gateway_tier_enforcement.py`;
`tests/unit/test_r35_recovery_seam.py`. **`chat_stream` and `rerank` are NOT touched in T1.**

**Nodes owned by T1** (exhaustive, no other task edits them except where noted):

* marker removed: `redteam::test_unclassifiable_provider_dispatch_increments_the_cloud_counter`,
  `redteam::test_retry_dispatches_increment_the_counter_once_each`
* inverted: `redteam::test_unclassifiable_provider_dispatch_is_not_counted` →
  `..._is_counted`; `redteam::test_counter_inequality_rows_are_measured` (T2 re-touches **row 2**
  only); `chaos::test_in_process_fault_is_a_typed_gateway_error`;
  `chaos_accounting::assert_no_earned_cost` + `test_faulted_call_logs_no_cost_and_earns_no_counter` →
  `..._earns_only_the_attempts_it_made` (T8 re-touches the per-fault attempts);
  `chaos_accounting::test_retry_then_success_logs_one_row_for_three_dispatches`;
  `exploratory::test_record_cloud_call_classifies_local_vs_cloud_slots`
* new: `redteam::test_every_dispatch_attempt_is_counted_inside_the_loop`;
  `redteam::test_an_unregistered_slot_primary_is_counted_as_cloud_on_a_permissive_tier` (§finding 6);
  `unit::TestChat::test_each_attempt_is_counted`
* edited only to keep them green under the new signature:
  `unit::TestEmbed::test_survives_cost_logging_failure`,
  `unit::test_strip_in_chat_path_integrates`, `unit::test_strip_all_empty_raises_empty_messages`,
  `tier::TestMaximumTierEnforcement::test_local_embed_proceeds`,
  `tier::TestPerformanceTierEnforcement::test_cloud_embed_allowed`,
  `tier::TestR35RecoveryTierBypass::test_balanced_allows_legal_cloud_model_override`,
  `tier::TestR35RecoveryTierBypass::test_chat_with_model_override_uses_override_for_tier_check`,
  `tier::TestR35RecoveryTierBypass::test_tier_matrix`,
  `r35::TestR35RecoverySeamBalanced::test_balanced_seam_allows_legal_cloud_fallback`;
  `tier::TestCloudCallCounter::test_cloud_call_counter_uses_override_prefix` (patch the real seam)

### Test edits (do all of them, then run Red)

1. `tests/redteam/test_redteam_egress_guard.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-027")` from
     `test_unclassifiable_provider_dispatch_increments_the_cloud_counter` (`:316`).
   * `test_unclassifiable_provider_dispatch_is_not_counted` (`:291`) → rename
     `test_unclassifiable_provider_dispatch_is_counted`. Replace its docstring (the "same state, two
     resolutions" framing is now false — one state, one resolution). Assertions:
     `gw._cloud_calls_made == 3` and `get_total_cloud_calls() == 3` (were `0`, `0`);
     `recorder.models() == ["bedrock/anthropic.claude-3-5-sonnet"] * 3` unchanged.
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-028")` from
     `test_retry_dispatches_increment_the_counter_once_each` (`:447`); assertions unchanged.
   * `test_counter_inequality_rows_are_measured` (`:383`): rows become
     `[("chat, 2 retries", 3, 3), ("chat, fallback model", 4, 1), ("chat, unknown slot primary", 0, 0),
     ("chat, unclassifiable provider", 3, 3)]`. `("chat, fallback model", 4, 1)` is the
     **intermediate** T1 value (the primary is `ollama` → local, so only the one `anthropic` fallback
     dispatch is counted); T2 changes that row to `(3, 0)`. Do **not** touch the
     `assert gw.chat(...) == "ok"` on the fallback run — T2 does.
   * New redteam test `test_every_dispatch_attempt_is_counted_inside_the_loop`: `FlakyDispatch(failures=2)`,
     cloud registry, `tier="performance"`, `mark_pii_available()`,
     `gw.chat("extraction", ...) == "ok"`, then `assert gw._cloud_calls_made == len(flaky.records) == 3`
     and `assert get_total_cloud_calls() == 3`.
   * New redteam test `test_an_unregistered_slot_primary_is_counted_as_cloud_on_a_permissive_tier`
     (**finding 6 — pins the documented behaviour change**): `monkeypatch.setattr(...load_registry, dict)`
     (an empty registry), `_w5.make_gateway(primary="mystery/model", tier="performance")`,
     `mark_pii_available()`, `DispatchRecorder(fail=True)`; `with contextlib.suppress(UnclassifiedProviderError): gw.chat(...)`;
     assert `len(recorder.records) == 3` and `gw._cloud_calls_made == 3` and
     `get_total_cloud_calls() == 3`. (Pre-T1: the post-dispatch counter reads the slot primary, finds
     nothing in the registry and counts 0.)
2. `tests/chaos/test_chaos_gateway_faults.py`
   * `test_in_process_fault_is_a_typed_gateway_error` (`:88`): `:102` →
     `assert flaky.gw._cloud_calls_made == flaky.seam.attempts`; `:103` →
     `assert get_total_cloud_calls() == flaky.seam.attempts`. Keep `cost_rows == []`.
3. `tests/chaos/test_chaos_accounting.py`
   * `assert_no_earned_cost(rows, gw, process_calls)` (`:64`) → add a 4th parameter `attempts: int`,
     and assert `gw._cloud_calls_made == attempts` / `process_calls == attempts` (both were `== 0`).
     Keep `rows == []`.
   * `test_faulted_call_logs_no_cost_and_earns_no_counter` (`:75`) → rename
     `test_faulted_call_logs_no_cost_and_earns_only_the_attempts_it_made`; call the helper with
     `flaky.seam.attempts`. **Leave `assert flaky.seam.attempts == EXPECTED_RETRY_ATTEMPTS` alone** —
     T8 replaces it with the per-fault table.
   * `test_retry_then_success_logs_one_row_for_three_dispatches` (`:110`): `:132` →
     `assert flaky.gw._cloud_calls_made == 3`; `:133` → `assert get_total_cloud_calls() == 3`; rewrite
     the docstring so it no longer calls the counter asymmetry "RT-028 … not re-opened here" — the
     ledger cross-check `len(rows) == 1` stays, the counter no longer equals it.
   * `test_negative_control_accounting_oracle_rejects_a_real_cost_row` (`:418`): update the helper
     call to pass `attempts`; its `pytest.raises(..., match="logged a cost")` still fires first.
4. `tests/exploratory/test_explore_egress_counter.py::test_record_cloud_call_classifies_local_vs_cloud_slots`
   (`:333`): `:375` `== 2` → `== 3` with the message "an unclassifiable provider now resolves as
   cloud, the same verdict the tier gate reaches"; `:379` `== 3` → `== 4`; `:383` `== 3` → `== 4`.
   (`:369`'s `== 2` for the **unresolvable slot** stays `2` — with a `None` prefix the counter still
   short-circuits; see the documented change below.)
5. `tests/unit/test_gateway_tier_enforcement.py::TestCloudCallCounter::test_cloud_call_counter_uses_override_prefix`
   (`:723`): the counter moves inside `_call_with_fallback`, so
   `monkeypatch.setattr(gw, "_call_with_fallback", lambda *a, **k: _chat_response("ok"))` (`:749`)
   bypasses it. **Delete that line**, patch the real dispatch seam instead —
   `monkeypatch.setattr("openreview_cli.gateway.router.completion", lambda **kw: _chat_response("ok"))`
   — and keep `assert gw.privacy_report().cloud_calls_made == 1`.
6. Every replacement fake for `_call_with_fallback` must accept the two new keyword-only arguments.
   Mechanically add `**_kw: Any` to each signature below. Nothing else changes.
   * `tests/unit/test_gateway_router.py` `TestEmbed::test_survives_cost_logging_failure` (`:243`),
     `test_strip_in_chat_path_integrates` (`:835`), `test_strip_all_empty_raises_empty_messages` (`:913`).
   * `tests/unit/test_gateway_tier_enforcement.py`
     `TestMaximumTierEnforcement::test_local_embed_proceeds` (`:122`),
     `TestPerformanceTierEnforcement::test_cloud_embed_allowed` (`:180`),
     `TestR35RecoveryTierBypass::test_balanced_allows_legal_cloud_model_override` (`:382`),
     `TestR35RecoveryTierBypass::test_chat_with_model_override_uses_override_for_tier_check` (`:450`),
     `TestR35RecoveryTierBypass::test_tier_matrix` (`:596`).
   * `tests/unit/test_r35_recovery_seam.py::TestR35RecoverySeamBalanced::test_balanced_seam_allows_legal_cloud_fallback`
     (`:201-206`; the fake is a local `def`, **not** a class-level patch).
   Leave alone: `_assert_dispatch_not_reached(*args, **kw)`, every `lambda *a, **k: ...`, and
   `patch.object(Gateway, "_call_with_fallback", return_value=...)` in
   `tests/integration/test_prompt_gateway.py:78-86`.
7. `tests/unit/test_gateway_router.py::TestChat` — new fast unit test `test_each_attempt_is_counted`
   (the socket-free twin of the redteam anchor): patch
   `openreview_cli.gateway.router.completion` with a fake that raises `RuntimeError("transient")` on
   the first two calls and then returns `_MockCompletionResponse("ok")` for
   `gw = _gateway(tmp_path, monkeypatch, COMMON_CONFIG)`, `gw.chat("reasoning", ...)`;
   assert the fake saw 3 calls and `gw._cloud_calls_made == 3`.

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/redteam/test_redteam_egress_guard.py::test_unclassifiable_provider_dispatch_increments_the_cloud_counter \
  tests/redteam/test_redteam_egress_guard.py::test_unclassifiable_provider_dispatch_is_counted \
  tests/redteam/test_redteam_egress_guard.py::test_retry_dispatches_increment_the_counter_once_each \
  tests/redteam/test_redteam_egress_guard.py::test_counter_inequality_rows_are_measured \
  tests/redteam/test_redteam_egress_guard.py::test_every_dispatch_attempt_is_counted_inside_the_loop \
  tests/redteam/test_redteam_egress_guard.py::test_an_unregistered_slot_primary_is_counted_as_cloud_on_a_permissive_tier \
  tests/chaos/test_chaos_gateway_faults.py::test_in_process_fault_is_a_typed_gateway_error \
  tests/chaos/test_chaos_accounting.py::test_faulted_call_logs_no_cost_and_earns_only_the_attempts_it_made \
  tests/chaos/test_chaos_accounting.py::test_retry_then_success_logs_one_row_for_three_dispatches \
  tests/exploratory/test_explore_egress_counter.py::test_record_cloud_call_classifies_local_vs_cloud_slots \
  tests/unit/test_gateway_router.py::TestChat::test_each_attempt_is_counted
=> 19 failed   (1+1+1+1+1+1 + 4 + 6 + 1 + 1 + 1)
```

### Source edits (`src/openreview_cli/gateway/router.py`)

1. `_call_with_fallback` — new signature (the only signature change in the seam):

```python
    def _call_with_fallback(
        self,
        slot: str,
        call_fn: Any,
        call_kwargs: dict[str, Any],
        *,
        call_type: str = "llm",               # "llm" | "embedding" | "reranking"
        provider_prefix: str | None = None,   # the prefix of the model actually dispatched
    ) -> Any:
```

2. Body — in this order:

```python
        cfg = self._get_slot_config(slot)
        provider = provider_prefix or cfg["primary"].split("/", 1)[0]
        fallback_cfg = self._config.get("gateway", {}).get("fallback", {})
        retries: int = fallback_cfg.get("retries", 2)
        retry_delay: float = fallback_cfg.get("retry_delay", 1.0)
        timeout: int = fallback_cfg.get("timeout", 60)
        call_kwargs["timeout"] = timeout          # T5 turns this into setdefault(...)

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            # C1/#147: the counter counts DISPATCHES, not logical calls. The count
            # sits before the try, so an attempt that raised is still counted; the
            # value is therefore an upper bound on egress, never an under-report.
            self._record_cloud_call(slot, provider_prefix=provider)
            try:
                return call_fn(**call_kwargs)
            except Exception as e:
                last_error = e
                if attempt < retries:
                    time.sleep(retry_delay)

        fallback = cfg.get("fallback")
        if slot in PRIMARY_ONLY_SLOTS or not fallback:
            if last_error is not None:
                classified = self._classify_error(last_error, provider)
                raise classified from last_error
            raise AllProvidersFailedError("All providers failed")

        fallback_prefix = fallback.split("/", 1)[0]
        call_kwargs["model"] = fallback
        self._record_cloud_call(slot, provider_prefix=fallback_prefix)
        try:
            return call_fn(**call_kwargs)
        except Exception as e:
            classified = self._classify_error(e, provider)
            raise classified from e
```

   (T2 will gate/retarget the fallback inside this branch; T1 only adds the count.)
3. `_record_cloud_call` (`:813`) — keep the **name and signature** verbatim
   (`def _record_cloud_call(self, slot: str, provider_prefix: str | None = None) -> None`; it is
   called directly by `tier::TestCloudCallCounter` (`:277`, `:297`) and
   `exploratory::test_record_cloud_call_classifies_local_vs_cloud_slots`, and patched on instances by
   `tests/unit/test_gateway_router.py:909,1058`,
   `tests/integration/test_gateway_streaming.py:53`). Flip only the `except ValueError` branch and
   refresh the docstring:

```python
        try:
            klass = classify_provider(info)
        except ValueError:
            # One state, one resolution (#143): _enforce_tier treats an
            # unclassifiable provider as cloud, so the counter must too.
            klass = "cloud"
        if klass == "cloud":
            self._cloud_calls_made += 1
            record_cloud_call()
```

4. Delete the **two** post-dispatch counter calls that are now dead: `chat` `:630` and `embed` `:746`
   (the `self._record_cloud_call(...)` lines plus their `# R3-5: …` comments).
   **Leave `chat_stream`'s `:674` line and its comment alone — T5 deletes it. Leave `rerank`'s `:808`
   line and its try/except alone — T7 deletes it.**
5. Call sites (T1 edits **`chat` and `embed` only**):
   * `chat`: `response = self._call_with_fallback(slot, completion, call_kwargs, provider_prefix=override_prefix)`
   * `embed`: `response = self._call_with_fallback(slot, embedding, call_kwargs, call_type="embedding")`
   * **`chat_stream` is unchanged in T1** (still `response = completion(**call_kwargs)` +
     post-dispatch `self._record_cloud_call(slot, provider_prefix=override_prefix)`); T5 owns it.
   * **`rerank` is unchanged in T1**; T7 owns it.
6. Refresh the module docstrings whose claims are now stale: `tests/redteam/_w5_probe.py:6-10` and
   `tests/redteam/test_redteam_egress_guard.py:11-16` say a swallowed `AssertionError` stays invisible
   because the counter is never reached — that reasoning must be restated (the *record* is what makes
   the dispatch observable; the counter now also counts it).

**Green.** Re-run the Red command → **19 passed**. Then the no-regression run:

```
uv run pytest -q -p no:randomly tests/redteam tests/chaos \
  tests/exploratory/test_explore_egress_counter.py tests/unit/test_gateway_router.py \
  tests/unit/test_gateway_tier_enforcement.py tests/unit/test_r35_recovery_seam.py \
  tests/integration/test_gateway_streaming.py
=> 0 failed.  Nodes still carrying an xfail marker report xfailed (never FINISHED-as-failure):
   tests/redteam  RT-029 (T2), RT-030 x2 + RT-032 (out of scope, stay)
   tests/chaos    RT-038 x2 (T4), RT-040 x2 (T5), RT-041 x2 (T6), RT-043 x2 (T8),
                  RT-047, RT-048, RT-049 (out of scope, stay; RT-047/048 are @NOT_ROOT-only)
```

---

## T2 — Gate and retarget the fallback model

**Goal.** The model that will actually hit the network passes the same tier/PII gate as the primary,
and the fallback dispatch carries the fallback provider's `api_base` and **no primary-provider
kwargs** (the load-bearing half is the custom-provider `api_key`, D2).

**Issues.** #144.

**Files.** `src/openreview_cli/gateway/router.py`; `tests/redteam/test_redteam_egress_guard.py`;
`tests/unit/test_gateway_router.py`.

**Nodes owned by T2:** marker removed `redteam::test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier`;
inverted `redteam::test_fallback_model_is_dispatched_on_the_strict_tier` →
`redteam::test_a_cloud_fallback_model_is_blocked_on_the_strict_tier`;
`redteam::test_counter_inequality_rows_are_measured` **row 2 only** (T1 owns the rest);
new `unit::TestChat::test_fallback_dispatch_carries_the_fallback_providers_api_base_and_drops_the_primarys_key`.

### Test edits

1. `tests/redteam/test_redteam_egress_guard.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-029")` from
     `test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier` (`:493`).
   * `test_fallback_model_is_dispatched_on_the_strict_tier` (`:465`) → rename
     `test_a_cloud_fallback_model_is_blocked_on_the_strict_tier`. Replace
     `assert gw.chat(...) == "ok"` with
     `with pytest.raises(NoMatchingProviderError): gw.chat("extraction", [{"role": "user", "content": "hi"}])`;
     `assert runner.models() == ["ollama/qwen3:8b"] * 3, runner.summary()` (no `anthropic/...`);
     keep `assert runner.records[-1].api_base == _w5.OLLAMA_BASE_URL`; keep
     `assert gw._cloud_calls_made == 0`.
   * `test_counter_inequality_rows_are_measured` — **second touch on a T1 node**: change row 2 to
     `SiteRow("chat, fallback model", 3, 0)` and change the fallback run's
     `assert gw.chat(...) == "ok"` to `with pytest.raises(NoMatchingProviderError): gw.chat(...)`
     (the fallback is tier-blocked, so the row is 3 local dispatches, 0 cloud counts — not 4/1).
     Docstring: the row set now measures *dispatches vs increments*, not violations. T2 may rename the
     node `test_counter_equals_dispatch_at_every_site`.
2. `tests/unit/test_gateway_router.py::TestChat` — new test
   `test_fallback_dispatch_carries_the_fallback_providers_api_base_and_drops_the_primarys_key`
   (the positive twin of the inverted node **and the D2 anchor — it must use a `source="custom"`
   primary so the pop discriminates**, §0.3 D2):
   monkeypatch `openreview_cli.gateway.router.load_registry` to return a two-entry registry where the
   primary prefix (`openai`) is `ProviderInfo(name="openai", env_key="OPENAI_API_KEY",
   base_url="https://api.openai.com/v1", source="custom", is_local=False,
   capabilities=Capability(reasoning=True))` and `anthropic` is
   `ProviderInfo(name="anthropic", env_key="ANTHROPIC_API_KEY", base_url="https://api.anthropic.com/v1",
   is_local=False, capabilities=Capability(reasoning=True))`. Patch
   `openreview_cli.gateway.router.completion` with a fake that appends
   `{"model": kw["model"], "api_base": kw.get("api_base"), "api_key": kw.get("api_key")}` and raises
   `RuntimeError("primary failed")` on the first 3 calls, then returns
   `_MockCompletionResponse("from fallback")`; `gw = _gateway(tmp_path, monkeypatch, COMMON_CONFIG)`
   (`reasoning.primary = openai/gpt-4`, `reasoning.fallback = anthropic/claude-3`, tier `performance`;
   the module autouse `_mark_pii_available` fixture satisfies the PII gate). Assert
   `gw.chat("reasoning", ...) == "from fallback"`,
   `seen[0]["api_key"] == "sk-test"` (**proves the primary really carried a key** — without this the
   anchor is vacuous), `seen[0]["api_base"] == "https://api.openai.com/v1"`,
   `seen[-1]["model"] == "anthropic/claude-3"`,
   `seen[-1]["api_base"] == "https://api.anthropic.com/v1"`, `seen[-1]["api_key"] is None`.

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/redteam/test_redteam_egress_guard.py::test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier \
  tests/redteam/test_redteam_egress_guard.py::test_a_cloud_fallback_model_is_blocked_on_the_strict_tier \
  tests/redteam/test_redteam_egress_guard.py::test_counter_inequality_rows_are_measured \
  tests/unit/test_gateway_router.py::TestChat::test_fallback_dispatch_carries_the_fallback_providers_api_base_and_drops_the_primarys_key
=> 4 failed
   (blocked-node `assert ... == []` vs `['anthropic/claude-3-5-haiku']`; rows 4/1 vs 3/0;
    the fallback dispatch still carries `api_key='sk-test'` and the primary's `api_base`)
```

### Source edits (`router.py`)

1. In `_call_with_fallback`'s fallback branch, replace the two lines
   `fallback_prefix = fallback.split("/", 1)[0]` and `call_kwargs["model"] = fallback` with the
   **collapsed** gate + retarget (A2: no new helper names):

```python
        fallback_prefix = fallback.split("/", 1)[0]
        # Same gate as the primary, against the ACTUAL model (router.py:240-244).
        self._enforce_tier(slot, call_type, provider_prefix=fallback_prefix)
        # Undo the primary's provider block, then apply the fallback's. Dropping the
        # primary's keys is load-bearing for a `source == "custom"` primary: litellm
        # honours an explicit `api_key` over the provider env var, so leaving it in
        # would send the PRIMARY's credential to the fallback provider's host (#144).
        primary = self._resolve_provider_info(slot)
        if primary is not None:
            for field in primary.credentials:
                call_kwargs.pop(field.litellm_param, None)
        call_kwargs.pop("api_base", None)
        call_kwargs.pop("api_key", None)
        info = load_registry().get(fallback_prefix)
        if info is not None and info.base_url:
            call_kwargs["api_base"] = info.base_url
        if info is not None and info.credentials:
            self._apply_provider_credentials(info, call_kwargs)
        call_kwargs["model"] = fallback
        self._record_cloud_call(slot, provider_prefix=fallback_prefix)
```

`_enforce_tier(slot, call_type, provider_prefix=...)` already fails closed for an unknown or
unclassifiable prefix (`:249-282`), so nothing new is written in the gate. Do **not** extract
`_provider_kwargs_for` and do **not** touch the `model=` override path (RT-032 stays out of scope).

**Green.** Re-run the Red command → **4 passed**. Then the no-regression run:

```
uv run pytest -q -p no:randomly tests/redteam tests/unit/test_gateway_router.py \
  tests/unit/test_gateway_tier_enforcement.py
=> 0 failed.  Still xfailed: tests/redteam RT-030 x2, RT-032 (out of scope).
   This is where §0.5's TestChat::test_falls_back_to_fallback_model either survives or is a real
   regression.
```

---

## T3 — Redact credentials before every user surface

**Goal.** A credential reflected in an upstream error body never reaches stderr/stdout/logs.

**Issues.** #146.

**Files.** `src/openreview_cli/gateway/redaction.py`; `src/openreview_cli/gateway/router.py`;
`src/openreview_cli/app.py`; `tests/unit/test_gateway_redaction.py` (add only).

**Nodes owned by T3:** marker removed
`redteam_setup::test_gateway_test_does_not_echo_key_material_from_a_provider_error`;
new `unit::TestRedactText` (3 nodes).

### Test edits

1. `tests/unit/test_gateway_redaction.py` — append a `TestRedactText` class (the TDD anchor for
   §4.5-4 and for the D1/A3 decisions). Assertions are correct against the **real** `redact_text`.
   The rule is: `redact_key` keeps the first 4 characters. For the sk-shaped canary the matched token
   is `sk-test-CANARY-123`, so those 4 characters are the *matched pattern's* prefix `sk-t`, and the
   literal `"sk-"` pass in `REDACT_PATTERNS` then re-masks them to `***t`. **Traced through the real
   function:** `redact_text("OPENAI_API_KEY=sk-test-CANARY-123")` = `_KEY_VALUE_RE` masks the token to
   `sk-t` + 14 stars → `"OPENAI_API_KEY"` → `OPEN**********` → `"sk-"` → `***` → final
   `OPEN**********=***t**************`. So the 4-visible-char rule is asserted as "the body beyond it
   is gone": `"CANAR" not in out` (and `"CANARY" not in out`), with `"***t" in out` as the positive
   control. The literal `"CANA" in out` / `"CANAR" not in out` pair only holds for a **registered**
   secret (which A3 cuts), so it is deliberately not asserted here.

```python
class TestRedactText:
    def test_masks_a_literal_env_name_and_an_sk_shaped_value(self) -> None:
        from openreview_cli.gateway import redaction

        out = redaction.redact_text("OPENAI_API_KEY=sk-test-CANARY-123")
        assert "sk-test-CANARY-123" not in out
        assert "OPENAI_API_KEY" not in out
        # redact_key keeps 4 chars; the body beyond them is gone.
        assert "CANAR" not in out and "CANARY" not in out
        assert "***t" in out   # positive control: the 4th char survived, the rest is masked

    def test_leaves_a_benign_non_secret_token_untouched(self) -> None:
        from openreview_cli.gateway import redaction

        assert "us-east-1" in redaction.redact_text("region us-east-1 unreachable")

    def test_a_credential_without_an_sk_pk_rk_shape_is_not_matched(self) -> None:
        """Documented limitation (A3): with `register_secret` cut, a credential with
        no `sk-`/`pk-`/`rk-` shape is NOT masked. This node pins the gap so a future
        fix is a deliberate change, not an accident."""
        from openreview_cli.gateway import redaction

        out = redaction.redact_text("boom: key CANARY-123-NONSECRETSHAPE rejected")
        assert "CANARY-123-NONSECRETSHAPE" in out
        assert "rejected" in out
```

2. Do **not** edit `TestRedactingFilter::test_redacts_matching_string_in_record` /
   `::test_redacts_multiple_patterns`; run them in the Red step to see them green **before** and
   **after** the change (they are the D1 guard).

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/redteam/test_redteam_gateway_setup.py::test_gateway_test_does_not_echo_key_material_from_a_provider_error \
  tests/unit/test_gateway_redaction.py::TestRedactText
=> 4 failed
   (the redteam probe reports `key material reached surface(s) {'stderr': 2}`;
    the three TestRedactText nodes fail with AttributeError: `redaction.redact_text` does not exist)
```

### Source edits

1. `src/openreview_cli/gateway/redaction.py` — add `redact_text` and delegate `RedactingFilter._redact`
   to it. No `register_secret`, no `_registered_secrets`, no `_MIN_SECRET_LEN`:

```python
def redact_text(text: str, patterns: Iterable[str] | None = None) -> str:
    """Redact credential-shaped material from any text bound for a user surface.

    The value pass runs first: a literal "sk-" pass would otherwise mask the
    prefix and leave the key body in place. `patterns` defaults to
    REDACT_PATTERNS; RedactingFilter passes its own list so a per-instance filter
    keeps its own patterns (tests/unit/test_gateway_redaction.py::TestRedactingFilter).
    """
    text = _KEY_VALUE_RE.sub(lambda m: redact_key(m.group(0)), text)
    for pat in patterns if patterns is not None else REDACT_PATTERNS:
        if pat and isinstance(pat, str) and pat in text:
            text = text.replace(pat, redact_key(pat))
    return text
```

   `RedactingFilter._redact` becomes `return redact_text(text, self._patterns)`; keep `filter()` and
   `install_on_root_handlers()` exactly as they are.
2. `src/openreview_cli/gateway/router.py`
   * Import `redact_text` alongside the existing `install_on_root_handlers, redact_key`.
   * `_classify_error` (`:464`): add one local `detail = redact_text(str(exc))` next to
     `msg = str(exc).lower()`, and use `detail` in place of `str(exc)` at the five construction sites
     (`AuthError` `:509`, `RateLimitError` `:511`, `ModelNotFoundError` `:513`, `ConnectionError`
     `:517`, and `_prefix(str(exc))` `:521`). Leave `msg`'s classification logic, the empty `message`
     strings (`""`) and the `_prefix` helper alone.
   * **No `register_secret` calls anywhere** (`_set_env_vars`, `_apply_provider_credentials`,
     `_get_litellm_kwargs` are untouched).
3. `src/openreview_cli/app.py`
   * Import `redact_text` next to `install_on_root_handlers` (module level, `:25`).
   * `gateway_test` (`:1702`): `typer.echo(f"Error: {redact_text(str(e))}", err=True)`.
   * **Do not change `_format_exception` (`:97-106`)** — out of scope (dropped; §Documented changes).

**Green.** Re-run the Red command → **4 passed**. Then:

```
uv run pytest -q -p no:randomly tests/unit/test_gateway_redaction.py \
  tests/unit/test_log_redaction_handlers.py tests/unit/test_api_key_redaction_e2e.py \
  tests/unit/test_cli_exception_formatting.py tests/redteam
=> 0 failed.  Still xfailed: tests/redteam RT-030 x2, RT-032.
```

Pay attention to `test_gateway_test_never_logs_the_key_even_at_debug_level` (§0.5): its `"***t"`
reachability assertion must survive (it does — `redact_text`'s literal `"sk-"` pass on the already
`redact_key`-masked `sk-t…` yields `***t…`), and `test_api_key_redaction_e2e`'s `_REDACTED_KEY_TAIL`
must still be present in the log.

---

## T4 — Decide "usable reply" before booking cost (`chat`)

**Goal.** A reply with no usable choice raises a typed gateway error naming the provider, and no
`cost_logs` row is written for it.

**Issues.** #149; the `chat` half of #150.

**Files.** `src/openreview_cli/gateway/router.py`; `tests/chaos/test_chaos_gateway_faults.py`;
`tests/chaos/test_chaos_accounting.py`; `tests/unit/test_gateway_router.py`.

**Nodes owned by T4:** marker removed
`faults::test_no_choice_response_is_a_typed_error_and_logs_no_cost` (2 params);
inverted `faults::test_no_choice_response_logs_a_cost_and_then_crashes_raw` →
`faults::test_no_choice_response_is_typed_and_leaves_no_cost_row` (2 params);
inverted `accounting::test_no_choice_reply_leaves_a_cost_row_while_the_call_fails` →
`accounting::test_no_choice_reply_leaves_no_cost_row_and_counts_the_dispatch`;
new `unit::TestChat::test_a_reply_with_no_usable_choice_is_a_typed_error` (3 params).

### Test edits

1. `tests/chaos/test_chaos_gateway_faults.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-038")` from
     `test_no_choice_response_is_a_typed_error_and_logs_no_cost` (`:155`).
   * `test_no_choice_response_logs_a_cost_and_then_crashes_raw` (`:170`) → rename
     `test_no_choice_response_is_typed_and_leaves_no_cost_row`. Replace
     `pytest.raises((AttributeError, IndexError))` with `pytest.raises(UnclassifiedProviderError)`;
     delete the `assert not isinstance(exc_info.value, GatewayError)` line; replace
     `rows`/`len(rows) == 1`/`rows[0][0]`/`rows[0][2]` with
     `assert _w7.cost_rows(flaky.state.db_path) == []`; keep
     `assert flaky.gw._cloud_calls_made == 1, "the counter did count the dispatch"` (C2).
     Delete the now-unused `EXPECTED_NO_CHOICE_COST` dict (`:151`; its only consumer was this node)
     and refresh the module docstring/comment block at `:142-151`.
2. `tests/chaos/test_chaos_accounting.py::test_no_choice_reply_leaves_a_cost_row_while_the_call_fails`
   (`:136`) → rename `test_no_choice_reply_leaves_no_cost_row_and_counts_the_dispatch`:
   `pytest.raises(UnclassifiedProviderError)` (was `AttributeError`);
   `assert _w7.cost_rows(flaky.state.db_path) == []` (was `len(rows) == 1` and
   `rows[0] == (CLOUD_PRIMARY, "openai", 0)`); add `assert flaky.gw._cloud_calls_made == 1`.
   Refresh the docstring and note in the PR that `draft/evidence/RT-039.txt` is superseded.
3. `tests/unit/test_gateway_router.py::TestChat` — new parametrised fast test (red today in all three
   params; the socket-free anchor for the three reply shapes):
   `test_a_reply_with_no_usable_choice_is_a_typed_error` over `["none", "empty_choices", "no_message"]`;
   patch `openreview_cli.gateway.router.completion` to return, respectively, `None`,
   `_types.SimpleNamespace(choices=[])`, and
   `_types.SimpleNamespace(choices=[_types.SimpleNamespace(message=None)])`; with
   `gw = _gateway(tmp_path, monkeypatch, COMMON_CONFIG)` call `gw.chat("extraction", ...)` (use
   `extraction`, which has no fallback) and assert `isinstance(exc_info.value, UnclassifiedProviderError)`
   and `"openai" in str(exc_info.value)`; also assert `gw._cloud_calls_made == 1` and that
   `gw._cost_tracker.log_call` was never called
   (`monkeypatch.setattr(gw._cost_tracker, "log_call", MagicMock())`).

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/chaos/test_chaos_gateway_faults.py::test_no_choice_response_is_a_typed_error_and_logs_no_cost \
  tests/chaos/test_chaos_gateway_faults.py::test_no_choice_response_is_typed_and_leaves_no_cost_row \
  tests/chaos/test_chaos_accounting.py::test_no_choice_reply_leaves_no_cost_row_and_counts_the_dispatch \
  tests/unit/test_gateway_router.py::TestChat::test_a_reply_with_no_usable_choice_is_a_typed_error
=> 8 failed   (2 + 2 + 1 + 3; `AttributeError: 'NoneType' object has no attribute 'choices'` /
   `IndexError: list index out of range`, and a cost row written)
```

### Source edits (`router.py`)

1. **Inline** the usable-reply guard in `chat` (A2/audit: one call site ⇒ no new method). `chat`'s tail
   becomes, in this exact order:

```python
        response = self._call_with_fallback(
            slot, completion, call_kwargs, provider_prefix=override_prefix
        )
        # #149/#150: validate the reply BEFORE the cost row, so a reply the caller can
        # never read leaves no ledger entry. Reuses UnclassifiedProviderError (the
        # existing _classify_error catch-all) instead of adding a class; the message
        # carries the provider name, mirroring _classify_error's "[provider] detail".
        reply_provider = override_prefix or provider_prefix
        choices = getattr(response, "choices", None)
        if not choices:
            raise UnclassifiedProviderError(f"[{reply_provider}] reply carried no usable choice")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise UnclassifiedProviderError(f"[{reply_provider}] reply choice carried no message")
        content = getattr(message, "content", None) or ""
        # Cost logging must never block the AI call (T030) …
        try:
            self._cost_tracker.log_call(
                session_id, slot, call_kwargs["model"], provider_prefix, response
            )
        except Exception as cost_err:
            logger.warning("Cost logging failed (non-fatal): %s", cost_err)
        return content
```

   (`None`, `choices == []` and a missing `message` all map to the typed error; `content=None` still
   returns `""`.) This is the **only** reordering in `chat`. Do not touch `embed`/`rerank`'s
   `response.data` / `response.results` derefs (design §7.6: no probe covers them).

**Green.** Re-run the Red command → **8 passed**. Then:

```
uv run pytest -q -p no:randomly tests/chaos tests/unit/test_gateway_router.py -m "not memory" --reruns 0
=> 0 failed.  Still xfailed: tests/chaos RT-040 x2 (T5), RT-041 x2 (T6), RT-043 x2 (T8),
   RT-047/RT-048/RT-049 (out of scope).
```

---

## T5 — The stream dispatch joins the retry/classification policy

**Goal.** A stream dispatch failure is retried and falls back like `chat`, and a failure during
iteration escapes as a typed gateway error naming the provider.

**Issues.** #151. (The truncation *reporting* and the stream cost row are T6; this task only stops raw
litellm types escaping.)

**Files.** `src/openreview_cli/gateway/router.py`; `tests/chaos/_w7_probe.py`;
`tests/chaos/test_chaos_gateway_faults.py`; `tests/chaos/test_chaos_accounting.py`.

**Nodes owned by T5:** marker removed `faults::test_stream_dispatch_failure_is_a_typed_gateway_error`
(2 params); inverted `faults::test_stream_dispatch_failure_escapes_as_a_raw_litellm_error` →
`faults::test_stream_dispatch_failure_is_a_typed_gateway_error_and_logs_no_cost` (2 params);
inverted `accounting::test_chat_stream_disables_retries_and_never_tries_the_fallback` →
`accounting::test_chat_stream_retries_then_tries_the_fallback`. **No other task edits `chat_stream`'s
dispatch or `router.py:674`.**

### Harness + test edits

1. `tests/chaos/_w7_probe.py::streaming_gateway` (`:586-623`) — **harness, not product**: add
   `"fallback": {"retries": 2, "retry_delay": 0.0, "timeout": 5}` next to the `models` block in
   `gw._config["gateway"]` (it currently has no `gateway.fallback` block, so the `provider_down`
   stream case would sleep `retry_delay = 1.0 s`). Everything else in `_w7_probe` stays: the real SSE
   server, `CHUNK`/`CHUNK2`/`DONE`, `StreamOutcome`, `collect_stream`.
2. `tests/chaos/test_chaos_gateway_faults.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-040")` from
     `test_stream_dispatch_failure_is_a_typed_gateway_error` (`:292`); assertions unchanged (both
     params, `malformed_body` and `provider_down`).
   * `test_stream_dispatch_failure_escapes_as_a_raw_litellm_error` (`:307`) → rename
     `test_stream_dispatch_failure_is_a_typed_gateway_error_and_logs_no_cost`. Replace
     `assert not isinstance(outcome.caught, GatewayError)` with
     `assert isinstance(outcome.caught, GatewayError)`; delete the
     `type(outcome.caught).__module__.startswith("litellm")` assertion; keep
     `assert gw._cloud_calls_made == 0`, and keep the `provider_down`-only block
     (`port_is_closed`, `request_hits == 0`, `cost_rows == []`). Update the docstring: the two faults
     surface at different times (eager dispatch vs. first `next()`), which is why both the dispatch
     seam and `_iter_stream` are fixed.
3. `tests/chaos/test_chaos_accounting.py::test_chat_stream_disables_retries_and_never_tries_the_fallback`
   (`:369`) → rename `test_chat_stream_retries_then_tries_the_fallback`. Replace the body after
   `collect_stream` with: `assert outcome.caught is not None`; `assert isinstance(outcome.caught, GatewayError)`
   (delete both `type(outcome.caught).__name__ == "ConnectionError"` and `not isinstance(outcome.caught, GatewayError)`);
   `assert flaky.seam.attempts == 4`; `assert flaky.seam.models == [CLOUD_PRIMARY] * 3 + ["anthropic/claude-3-5-haiku"]`;
   `assert _w7.cost_rows(flaky.state.db_path) == []` (unchanged); `assert flaky.gw._cloud_calls_made == 4`.
   Rewrite the docstring ("sharp edge 10" is fixed; both prefixes in that registry are cloud — the
   absent `anthropic` entry takes the fail-closed unknown-prefix count).

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_dispatch_failure_is_a_typed_gateway_error \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_dispatch_failure_is_a_typed_gateway_error_and_logs_no_cost \
  tests/chaos/test_chaos_accounting.py::test_chat_stream_retries_then_tries_the_fallback
=> 5 failed   (2 + 2 + 1; raw `litellm.MidStreamFallbackError` / `litellm.InternalServerError`
   escape for `malformed_body`; `attempts == 1` for the retry node)
```

### Source edits (`router.py`)

1. `_call_with_fallback`: `call_kwargs["timeout"] = timeout` → `call_kwargs.setdefault("timeout", timeout)`,
   with the comment "chat_stream sets the dual httpx.Timeout (FR-6 15s/45s) BEFORE dispatch;
   overwriting it with an int would destroy the idle-timeout contract and break
   test_stream_timeout_is_dual_not_single".
2. `chat_stream` (`:672`): replace `response = completion(**call_kwargs)` with
   `response = self._call_with_fallback(slot, completion, call_kwargs, provider_prefix=override_prefix)`.
   Delete the post-dispatch counter at `:674` **and its `# R3-5: …` comment** (the seam now counts the
   dispatch). Keep `call_kwargs["num_retries"] = 0` and update its comment to
   "# litellm must not retry underneath; the gateway owns the retry layer". Do **not** change the
   dual-timeout block and do **not** touch the cost block at `:675-684` (T6 moves it).
3. `_iter_stream`'s `except Exception as exc:` block (`:694-717`) — this order, exactly:
   1. the existing `httpx.ConnectTimeout` branch (unchanged),
   2. the existing `httpx.ReadTimeout` branch (unchanged),
   3. the existing litellm-timeout heuristic branch (unchanged),
   4. `raise self._classify_error(exc, provider_prefix) from exc` — replaces the bare `raise` at `:717`.
   **No `isinstance(exc, GatewayError)` guard** (the truncation `ConnectionError` is raised *after* the
   try/except in T6, so a guard is unnecessary and would only re-classify a future in-loop GatewayError).
   **No new import** (`GatewayError` is not needed once the guard is gone).

**Green.** Re-run the Red command → **5 passed**. Then:

```
uv run pytest -q -p no:randomly tests/chaos -m "not memory" --reruns 0
=> 0 failed.  Still xfailed: RT-041 x2 (T6), RT-043 x2 (T8), RT-047/RT-048/RT-049 (out of scope).
   test_healthy_local_stream_completes_with_chunks_and_done must stay green here, and
   tests/unit/test_gateway_router.py::test_stream_idle_timeout_cuts_stalled_provider (a separate,
   ~45 s socket test) must still pass. tests/integration/test_gateway_streaming.py must stay green.
```

---

## T6 — The terminal marker: truncation is not success, and a truncated stream books no cost

**Goal.** `done` is emitted only when the provider itself signalled a finish reason; an unterminated
stream raises the typed gateway error naming the provider and leaves no `cost_logs` row.

**Issues.** #152; the `chat_stream` half of #150.

**Files.** `src/openreview_cli/gateway/router.py`; `tests/helpers/stream_doubles.py` (new, shared);
`tests/redteam/_w5_probe.py`; `tests/chaos/test_chaos_gateway_faults.py`;
`tests/unit/test_gateway_router.py`.

**Nodes owned by T6:** marker removed
`faults::test_stream_without_a_completion_marker_is_not_reported_as_done` (2 params);
inverted `faults::test_stream_without_a_completion_marker_is_reported_as_done_today` →
`faults::test_stream_without_a_completion_marker_is_reported_as_truncated` (2 params);
`unit::test_chat_stream_survives_cost_logging_failure` + `unit::test_chat_stream_yields_chunks_and_done`
(terminated-stream double; event lists unchanged); `unit::test_stream_timeout_is_dual_not_single`
(wrap in `pytest.raises`); new `unit::test_iter_stream_raises_for_a_stream_with_no_terminal_marker`
and `unit::test_a_truncated_stream_writes_no_cost_row_and_a_terminal_one_writes_exactly_one`.

### Harness + test edits

1. **One stream double, defined once in an importable helper** (`tests/helpers/stream_doubles.py`), used
   by the redteam harness stream mode **and** all unit stream tests. `finish_reason` lives on the
   **choice** object (that is where `_iter_stream` reads `chunk.choices[0]`), not on the chunk:

```python
class StreamDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content

class StreamChoice:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.delta = StreamDelta(content)
        self.finish_reason = finish_reason

class StreamChunk:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.choices = [StreamChoice(content, finish_reason)]

class TerminatedStream:
    """What litellm returns for a stream that ended with a provider finish_reason."""

    def __init__(self, chunks: list[StreamChunk], terminal: str = "stop") -> None:
        self.chunks = chunks
        self.received_finish_reason: str | None = terminal

    def __iter__(self) -> Iterator[StreamChunk]:
        return iter(self.chunks)

class TruncatedStream:
    """A stream whose provider never sent a finish_reason (no terminal attribute)."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[StreamChunk]:
        return iter(self.chunks)
```

   (No `intermittent_finish_reason`: the predicate reads a single attribute, §0.3 D4.)
2. `tests/redteam/_w5_probe.py` — replace the stream branch of `DispatchRecorder.completion`
   (`:316`, currently `return iter([_Chunk("he"), _Chunk("llo")])`) and the `_Chunk`/`_StreamChoice`/
   `_Delta` doubles with imports from `tests.helpers.stream_doubles`; stream mode returns
   `TerminatedStream([StreamChunk("he"), StreamChunk("llo", finish_reason="stop")])`. This is what
   keeps `test_counter_equality_per_dispatch_site` (`chunks[-1].type == "done"`) honest instead of
   accidentally asserting the truncation path.
3. `tests/chaos/test_chaos_gateway_faults.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-041")` from
     `test_stream_without_a_completion_marker_is_not_reported_as_done` (`:333`); assertions unchanged.
   * `test_stream_without_a_completion_marker_is_reported_as_done_today` (`:353`) → rename
     `test_stream_without_a_completion_marker_is_reported_as_truncated`. Replace
     `assert outcome.caught is None` with
     `assert isinstance(outcome.caught, GatewayConnectionError)` (import
     `from openreview_cli.gateway.errors import ConnectionError as GatewayConnectionError`);
     `assert STREAM_PROVIDER in str(outcome.caught)`; `assert not outcome.completed`;
     `assert "done" not in outcome.events`; `assert _w7.cost_rows(gw._data_path) == []` (was
     `len(rows) == 1`); keep `request_hits(server) >= 1` and `gw._cloud_calls_made == 0`.
4. `tests/unit/test_gateway_router.py` — drop the local `_make_chunk` (`:937`); import
   `StreamChunk, TerminatedStream, TruncatedStream` from `tests.helpers.stream_doubles`:
   * `test_chat_stream_yields_chunks_and_done` (`:977`) and `test_chat_stream_survives_cost_logging_failure`
     (`:953`): the `completion` lambda returns
     `TerminatedStream([StreamChunk("Hello"), StreamChunk(" world", finish_reason="stop")])`. Event-list
     assertions stay **exactly** as they are; the "survives" node already asserts `done` is in the list
     — keep that and add a comment that the row is now on the post-terminal path.
   * `test_stream_timeout_is_dual_not_single` (`:995`): `fake_completion` returns `TruncatedStream([])`,
     which now raises on drain — wrap the drain in `with pytest.raises(GatewayConnectionError):` (import
     it from `openreview_cli.gateway.errors`) and keep both timeout assertions
     (`isinstance(t, httpx.Timeout)`, `t.connect == 15.0`, `t.read == 45.0`). This documents the
     empty-stream contract.
   * New `test_iter_stream_raises_for_a_stream_with_no_terminal_marker` (§4.5-5): with
     `gw = _build_gw()` (slot primary `anthropic/claude-3`), patch `router.completion` to return
     `TruncatedStream([StreamChunk("a"), StreamChunk("b")])`; drain through
     `gw.chat_stream("extraction", ...)` and assert `pytest.raises(GatewayConnectionError)` and
     `"anthropic" in str(exc.value)`.
   * New `test_a_truncated_stream_writes_no_cost_row_and_a_terminal_one_writes_exactly_one`
     (§4.5-6, socket-free twin of the chaos node): patch `gw._cost_tracker.log_call` with `MagicMock()`;
     drain a `TerminatedStream` → exactly one call and a final `done` event; drain a `TruncatedStream`
     → `pytest.raises(GatewayConnectionError)` and `log_call` was **not** called.

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_without_a_completion_marker_is_not_reported_as_done \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_without_a_completion_marker_is_reported_as_truncated \
  tests/unit/test_gateway_router.py::test_iter_stream_raises_for_a_stream_with_no_terminal_marker \
  tests/unit/test_gateway_router.py::test_a_truncated_stream_writes_no_cost_row_and_a_terminal_one_writes_exactly_one
=> 6 failed   (2 + 2 + 1 + 1; `outcome.completed` is True with `done` in the events,
   `caught is None`, and one `cost_logs` row written)
```

### Source edits (`router.py`)

1. New module-level helper (next to `classify_provider`, above `class Gateway`) — single predicate:

```python
def _stream_terminated(response: Any) -> bool:
    """True when the provider itself signalled a finish reason.

    litellm's CustomStreamWrapper fabricates ``finish_reason="stop"`` on EOF, so a
    CHUNK's finish_reason cannot be used — measured against
    tests/chaos/_w7_probe.LocalSSEServer: a truncated stream's last chunk carries
    'stop' while ``received_finish_reason`` stays None. ``intermittent_finish_reason``
    never differs from it in any measured mode, so only this one attribute is read.
    """
    return bool(getattr(response, "received_finish_reason", None))
```

2. `_iter_stream`: keep the `for chunk in response:` loop and the `try/except` (T5's classified
   `raise` stays inside the except). **After** the try/except block — i.e. only when the loop
   completed normally — add the terminal check, and **delete** the unconditional
   `yield StreamingOutputEvent(type="done")` from `_iter_stream`:

```python
        if not _stream_terminated(response):
            raise ConnectionError(
                provider_prefix,
                "stream ended without a terminal finish_reason (truncated or interrupted)",
            )
```

   Placing the check *after* the `try/except` is what removes the need for the `isinstance(exc,
   GatewayError)` guard. A failure direction that reports "incomplete", never "success", is the intent
   of #152; a provider that closes with `data: [DONE]` and no finish-reason-bearing chunk is a known,
   accepted fail-closed false positive (design §6).
3. `chat_stream`'s tail — delete the pre-drain cost block (`:675-684`, including the `# ponytail:`
   comment) and replace `yield from self._iter_stream(response, provider_prefix)` with:

```python
        for event in self._iter_stream(response, provider_prefix):
            yield event
        # #150: the row is written only after the terminal marker was seen, so a
        # truncated or never-yielding stream leaves none. Streaming rows are still
        # 0-cost (log_call reads .usage, absent on the dash wrapper) — noted for a
        # follow-up issue, out of scope here.
        try:
            self._cost_tracker.log_call(
                session_id, slot, call_kwargs["model"], provider_prefix, response
            )
        except Exception as cost_err:
            logger.warning("Cost logging failed (non-fatal): %s", cost_err)
        yield StreamingOutputEvent(type="done")
```

   **This is deviation D3**: `chat_stream` owns the cost row and `done`; `_iter_stream` keeps its
   two-parameter signature and stays a pure stream transform.

**Green.** Re-run the Red command → **6 passed**. Then:

```
uv run pytest -q -p no:randomly tests/chaos tests/unit/test_gateway_router.py tests/redteam \
  tests/integration/test_gateway_streaming.py
=> 0 failed.  Still xfailed: tests/chaos RT-043 x2 (T8), RT-047/RT-048/RT-049 (out of scope);
   tests/redteam RT-030 x2, RT-032 (out of scope).
   test_healthy_local_stream_completes_with_chunks_and_done and
   test_counter_equality_per_dispatch_site must both be green (the latter is the §5.1 harness check).
```

---

## T7 — `rerank` uses the configured retry loop

**Goal.** `gateway.fallback.retries` applies to `rerank` exactly as it does to `chat`/`embed`.

**Issues.** #153.

**Files.** `src/openreview_cli/gateway/router.py`; `tests/chaos/test_chaos_accounting.py`.

**Nodes owned by T7:** inverted `accounting::test_rerank_has_no_retry_loop` →
`accounting::test_rerank_uses_the_configured_retry_loop`.

### Test edits

`tests/chaos/test_chaos_accounting.py::test_rerank_has_no_retry_loop` (`:393`) → rename
`test_rerank_uses_the_configured_retry_loop`. Invert: `assert len(calls) == 3` (was `1`);
`assert "openai" in str(exc_info.value)` unchanged;
`assert _w7.cost_rows(flaky.state.db_path) == []` unchanged;
`assert flaky.gw._cloud_calls_made == 3` (was `0`). Rewrite the docstring ("sharp edge 11" is fixed:
`reranking` is in `PRIMARY_ONLY_SLOTS`, so the fallback branch stays unreachable while retries now
apply).

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/chaos/test_chaos_accounting.py::test_rerank_uses_the_configured_retry_loop
=> 1 failed   (`assert 1 == 3`)
```

### Source edits (`router.py`, `rerank` — `:788-808`)

Replace the `cfg`/`fallback_cfg`/`timeout`/`try-except` block and the trailing counter call with:

```python
        from litellm import rerank                        # local import: tests patch litellm.rerank

        cfg = self._get_slot_config(slot)
        call_kwargs: dict[str, Any] = {
            "query": query,
            "documents": documents,
            "top_n": top_n,
            **self._get_litellm_kwargs(slot),
        }
        response = self._call_with_fallback(
            slot, rerank, call_kwargs, call_type="reranking"
        )
        try:
            self._cost_tracker.log_call(
                session_id, slot, cfg["primary"], cfg["primary"].split("/", 1)[0], response
            )
        except Exception as cost_err:
            logger.warning("Cost logging failed (non-fatal): %s", cost_err)
        return [
            {"index": r["index"], "relevance_score": r["relevance_score"]} for r in response.results
        ]
```

Delete the now-dead `fallback_cfg`/`timeout` locals **and** the last post-dispatch
`self._record_cloud_call(slot)` (`:808`) — this is the fourth and final counter call site.
`timeout` now arrives from `_call_with_fallback`'s `setdefault("timeout", …)`, the same value today's
explicit `timeout=timeout` passed. **Keep `cfg["primary"]` as the `cost_logs` model argument and
`cfg["primary"].split("/")[0]` as the provider in the row and in errors** (`test_rerank_error_names_provider`
asserts `provider == "cohere"`; do not switch the row's model argument to `call_kwargs["model"]` — that
would be an unmotivated change, no probe requires it). Keep the `response.results` deref after the row
(design §7.6).

**Green.** Re-run the Red command → **1 passed**. Then:

```
uv run pytest -q -p no:randomly tests/chaos tests/unit/test_gateway_router.py
=> 0 failed.  Still xfailed: tests/chaos RT-043 x2, RT-047/RT-048/RT-049.
   Re-check tests/redteam/test_redteam_egress_guard.py::test_counter_equality_per_dispatch_site
   (rerank's four-site row is now counted by the seam, still `1 == 1`).
```

---

## T8 — Permanent errors short-circuit the retry loop

**Goal.** A 401/403 auth failure and a 404 not-found are dispatched once, not `retries + 1` times.

**Issues.** #154.

**Files.** `src/openreview_cli/gateway/router.py`; `tests/chaos/test_chaos_accounting.py`;
`tests/unit/test_gateway_router.py`.

**Nodes owned by T8:** marker removed `accounting::test_permanent_errors_are_not_retried` (2 params);
inverted `accounting::test_permanent_errors_are_retried_today` →
`accounting::test_permanent_errors_are_dispatched_once` (2 params);
`accounting::test_retry_shape_is_three_attempts_for_every_fault_class`;
`accounting::test_faulted_call_logs_no_cost_and_earns_only_the_attempts_it_made` (T1's node,
re-touched: per-fault attempts); new `unit::TestChat::test_a_permanent_error_is_dispatched_once`.

### Test edits

1. `tests/chaos/test_chaos_accounting.py`
   * Delete `@pytest.mark.xfail(strict=True, reason="RT-043")` from `test_permanent_errors_are_not_retried`
     (`:172`); assertions unchanged.
   * `test_permanent_errors_are_retried_today` (`:185`) → rename `test_permanent_errors_are_dispatched_once`:
     `assert flaky.seam.attempts == 1` (was `EXPECTED_RETRY_ATTEMPTS`); keep
     `assert isinstance(exc_info.value, GatewayError)`.
   * `test_retry_shape_is_three_attempts_for_every_fault_class` (`:157`): replace the single
     `dict.fromkeys(...)` assertion with a per-fault expectation — transient
     (`"timeout"`, `"rate_limit"`, `"five_hundred"`, `"five_hundred_three"`) `== 3`, permanent
     (`"auth"`, `"not_found"`) `== 1` — derived from the existing `PERMANENT_FAULTS` dict rather than
     repeating the literal names; rewrite the docstring ("every exception class — permanent ones
     included — is retried" is no longer true).
   * `test_faulted_call_logs_no_cost_and_earns_only_the_attempts_it_made` (T1's version): replace
     `assert flaky.seam.attempts == EXPECTED_RETRY_ATTEMPTS` with a per-fault expectation
     (`PERMANENT_FAULTS` → `1`, everything else → `EXPECTED_RETRY_ATTEMPTS`); keep passing
     `flaky.seam.attempts` to `assert_no_earned_cost`.
2. `tests/unit/test_gateway_router.py::TestChat` — new fast test (the socket-free anchor, §4.5-3):
   `test_a_permanent_error_is_dispatched_once`. Define `class _Unauthorized(Exception): status_code = 401`;
   patch `openreview_cli.gateway.router.completion` with a fake that appends `kw["model"]` to a list and
   always raises `_Unauthorized("unauthorized")`; on `gw = _gateway(tmp_path, monkeypatch, COMMON_CONFIG)`
   call **`gw.chat("extraction", ...)`** (the `extraction` slot has no fallback — the `reasoning` slot
   does, and C6 keeps one fallback attempt available, so `reasoning` would give 2). Assert
   `len(calls) == 1` and `pytest.raises(AuthError)`.

**Red.**

```
uv run pytest -q -p no:randomly \
  tests/chaos/test_chaos_accounting.py::test_permanent_errors_are_not_retried \
  tests/chaos/test_chaos_accounting.py::test_permanent_errors_are_dispatched_once \
  tests/chaos/test_chaos_accounting.py::test_retry_shape_is_three_attempts_for_every_fault_class \
  tests/chaos/test_chaos_accounting.py::test_faulted_call_logs_no_cost_and_earns_only_the_attempts_it_made \
  tests/unit/test_gateway_router.py::TestChat::test_a_permanent_error_is_dispatched_once
=> 8 failed, 4 passed
   (2 xfail-removed nodes + 2 renamed, all failing "a permanent error was retried 3 times";
    test_retry_shape... fails 1; test_faulted_call... fails only its `auth`/`not_found` params — 2
    of its 6 cases fail, the 4 transient cases pass; new unit node fails 1)
```

### Source edits (`router.py`)

1. In the retry loop, after `last_error = e` and before the `time.sleep` — **inline** the permanence
   test (audit: one call site ⇒ no helper method):

```python
                last_error = e
                # C6/#154: a retry cannot fix a bad credential or a missing model.
                # _classify_error already produces AuthError for 401/403/auth and
                # ModelNotFoundError for 404/not-found, so there is no second table.
                if isinstance(self._classify_error(e, provider), (AuthError, ModelNotFoundError)):
                    break
                if attempt < retries:
                    time.sleep(retry_delay)
```

   The count for the failed attempt already happened before the `try` (C1), so the single attempt is
   still counted. A configured fallback is still tried once afterwards (C6) — the `#154` probe node
   has no fallback configured, so `attempts == 1` holds.

**Out of scope for this task (do not do):** jitter, a total-time cap, treating 400 as permanent.
They would invalidate the green pins `test_retry_delay_is_fixed_with_no_jitter_or_cap` and
`test_retry_shape_is_three_attempts_for_every_fault_class`'s transient rows for no repro node (design C7).

**Green.** Re-run the Red command → **12 passed**. Then:

```
uv run pytest -q -p no:randomly tests/chaos -m "not memory" --reruns 0
=> 0 failed.  Still xfailed (permanent, out of scope): RT-047, RT-048, RT-049
   (RT-047/RT-048 are @NOT_ROOT-only and may report skipped instead of xfailed under root).
```

---

## Documented changes, limitations and PR notes

1. **Residual redaction gap (A3).** `redact_text` masks (a) tokens matching `_KEY_VALUE_RE`
   (`\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}`) and (b) literal `REDACT_PATTERNS`. A credential with **no**
   `sk-`/`pk-`/`rk-` shape (e.g. an AWS secret key) shown verbatim in an error body is **not** masked.
   `register_secret()` was cut, so this is the accepted boundary; pinned by
   `TestRedactText::test_a_credential_without_an_sk_pk_rk_shape_is_not_matched`. The #146 probe's
   canary has an `sk-` shape, so the regex pass alone un-xfails it. Record this in the PR.
2. **Unregistered-slot-primary behaviour change (fail-closed over-count).** `_call_with_fallback`
   passes a **non-None** `provider_prefix` to `_record_cloud_call` on every attempt
   (`provider_prefix or cfg["primary"].split("/")[0]`). Consequently a slot whose primary prefix is
   **not in the registry**, dispatched under a tier that permits it, is now counted as **cloud**
   (previously the counter read `_resolve_provider_info(slot)` → `None` → not counted). This is the
   same fail-closed direction as #143/#147 and is pinned by
   `redteam::test_an_unregistered_slot_primary_is_counted_as_cloud_on_a_permissive_tier`. As a direct
   consequence, the `provider_prefix is None` / `_resolve_provider_info` branch of `_record_cloud_call`
   is **dead in `src/`** after T1 (only tests call `_record_cloud_call` directly); its name/signature
   are kept because those tests and two instance patchers depend on them. Record in the PR.
3. **#149's "no counter increment" clause is superseded by C1** (design C2): the dispatch is counted
   (`_cloud_calls_made == 1`) and only the cost row and the raw crash are removed.
4. **D2 is custom-provider-scoped.** The `api_key` pop is load-bearing only for a `source == "custom"`
   primary; bundled providers declare no `credentials` and are not `custom`, so for them the pop is
   inert and only the `api_base` re-derivation is observable. The T2 anchor uses a `custom` primary so
   the fix is actually exercised.
5. **No `_format_exception` redaction change.** Only `_classify_error` and the `gateway test` echo are
   in scope for #146. `_format_exception` (`app.py:97-106`) is untouched; this plan does not claim it.
6. **Zero-cost streaming rows remain** (`CostTracker.log_call` reads `response.usage`, absent on the
   dash wrapper) — the row is now written only for a genuinely terminated stream, which is what #150
   asks. Real stream usage needs `litellm.stream_chunk_builder(chunks=response.chunks)` (out of scope,
   follow-up issue).
7. **Counter semantics are user-visible** ("Cloud calls: N" now counts attempts — an upper bound on
   egress, never an under-report). Record in the PR.
8. **`draft/evidence/RT-039.txt` is superseded** by T4's accounting node.

---

## Global verification — run in this exact order

```
# 0. hygiene: exactly the two plan docs are untracked; nothing else, no stray scratch
git status --porcelain

# 1. the 8 probe node ids (12 cases) — the ONLY --runxfail command (baseline was 12 failed).
#    By now the markers are gone; --runxfail is a guard: a leftover marker turns a silent skip
#    into a failure.
uv run pytest --runxfail -q -p no:randomly --no-header \
  tests/redteam/test_redteam_egress_guard.py::test_unclassifiable_provider_dispatch_increments_the_cloud_counter \
  tests/redteam/test_redteam_egress_guard.py::test_retry_dispatches_increment_the_counter_once_each \
  tests/redteam/test_redteam_egress_guard.py::test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier \
  tests/redteam/test_redteam_gateway_setup.py::test_gateway_test_does_not_echo_key_material_from_a_provider_error \
  tests/chaos/test_chaos_gateway_faults.py::test_no_choice_response_is_a_typed_error_and_logs_no_cost \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_dispatch_failure_is_a_typed_gateway_error \
  tests/chaos/test_chaos_gateway_faults.py::test_stream_without_a_completion_marker_is_not_reported_as_done \
  tests/chaos/test_chaos_accounting.py::test_permanent_errors_are_not_retried
# => 12 passed

# 2. the whole redteam + chaos trees. PLAIN run (no --runxfail): the out-of-scope xfails stay
#    xfailed and must NOT be counted as failures.
uv run pytest -q -p no:randomly tests/redteam tests/chaos
# => 0 failed, and exactly these PERMANENT xfails:
#      tests/redteam: RT-032, RT-030 x2
#      tests/chaos:   RT-047, RT-048, RT-049   (RT-047/048 @NOT_ROOT: skipped instead of xfailed under root)
#    Do NOT claim "0 xfailed" for either tree.

# 3. the chaos CI command
uv run pytest tests/chaos -q -m "not memory" --reruns 0

# 4. the affected unit / exploratory / integration files
uv run pytest -q -p no:randomly \
  tests/unit/test_gateway_router.py tests/unit/test_gateway_tier_enforcement.py \
  tests/unit/test_gateway_errors.py tests/unit/test_gateway_redaction.py \
  tests/unit/test_log_redaction_handlers.py tests/unit/test_api_key_redaction_e2e.py \
  tests/unit/test_cloud_call_counter.py tests/unit/test_cli_exception_formatting.py \
  tests/unit/test_r35_recovery_seam.py tests/exploratory/test_explore_egress_counter.py \
  tests/helpers/ tests/integration/test_gateway_streaming.py tests/integration/test_prompt_gateway.py \
  tests/integration/test_gateway_cli.py tests/integration/tui/test_status_bar_egress.py \
  tests/integration/tui/test_explore_egress_modal.py
# => all passed

# 5. the offline suites, both marker pools
uv run pytest -m fast -q
uv run pytest -m slow -q
uv run pytest -m "fast or slow" -q
# (memory tests are unrelated to this change and always run solo: uv run pytest -m memory -q)

# 6. lint, format, types, hooks — these are what the pre-commit hook runs
uv run ruff check . && uv run ruff format .
uv run mypy src/ tests/
uv run pre-commit run --all-files

# 7. heartbeat: the repo default (random order, no -p no:randomly) over the offline pool.
uv run pytest -m "fast or slow" -q
```

Expected end state: **12/12 probe cases green without `xfail` markers; the out-of-scope xfails
(RT-032, RT-030 ×2 in `tests/redteam`; RT-047, RT-048, RT-049 in `tests/chaos`) remain `xfailed` and
are the only expected non-passing nodes in those trees; everything else in
`tests/unit`/`tests/exploratory`/`tests/integration`/`tests/helpers` green; ruff + mypy + pre-commit
clean.**

---

## Risk register

| # | Risk | What would reveal it |
|---|---|---|
| R1 | `redact_text`'s literal-pattern pass mangles unrelated text: `"sk-"` is a literal `REDACT_PATTERNS` entry, so any message containing it becomes `***`-prefixed. Reached now from `_classify_error` **and** the `gateway test` echo (not `_format_exception`). | `uv run pytest -q tests/unit/test_cli_exception_formatting.py tests/redteam` and `uv run pytest -m "fast or slow" -q` |
| R2 | The residual gap (Documented change 1): a credential with no `sk-`/`pk-`/`rk-` shape is not masked. | `TestRedactText::test_a_credential_without_an_sk_pk_rk_shape_is_not_matched`; record in the PR |
| R3 | The unregistered-slot-primary case is now counted as cloud (fail-closed over-count), and `_record_cloud_call`'s `provider_prefix is None` branch is dead in `src/`. | `redteam::test_an_unregistered_slot_primary_is_counted_as_cloud_on_a_permissive_tier`; and `uv run pytest -q tests/unit/test_gateway_tier_enforcement.py tests/exploratory/test_explore_egress_counter.py` |
| R4 | The fallback branch pops the primary's credential kwargs but does **not** re-apply a custom-provider `api_key`/`openai-` rewrite for the fallback model: a custom provider used *as a fallback* now fails closed with a 401 instead of dispatching. Chosen deliberately (a 401 beats sending the primary's credential to another host); no probe covers a custom fallback. | Nothing today — record in the PR. If it ever matters: a redteam probe with a custom fallback + a credential-reflecting provider |
| R5 | The fallback dispatch's **failure** is still classified with the *primary's* provider prefix (`_classify_error(e, provider)`), so a fallback failure can be reported under the wrong provider name. Pre-existing behaviour; the probes never assert on it. | Record in the PR; a new probe asserting the provider name in a fallback-failure message |
| R6 | Truncation and idle-timeout now share one class (`gateway.errors.ConnectionError`), and that class maps to HTTP 503 → transient → `provider_fallback` in the recovery layer, so a mid-stream truncation *could* trigger a cross-provider re-dispatch after partial output. `chat_stream` is consumed nowhere in `src/`, so no recovery path sees it today. | `grep -rn "chat_stream" src/` → only `router.py` |
| R7 | A reply with no usable choice is now `UnclassifiedProviderError` (A1), which the recovery layer routes as **transient → provider_fallback** (via `review/_gateway.py::_GATEWAY_ERROR_HTTP_STATUS[UnclassifiedProviderError] = 503`). The design doc intended `user_guided_recovery`; the probe only requires `GatewayError` + the provider name. | `uv run pytest -q tests/chaos/test_chaos_accounting.py` (the coordinator routing tests) plus `review/_gateway.py:55-59` |
| R8 | Streaming now sleeps on retry: a stream config **without** a `gateway.fallback` block uses `retry_delay = 1.0`, so a `provider_down` stream case costs 2 s per parametrisation. Fixed by T5's §5.3 harness change; a product config without the block inherits the same delay. | `uv run pytest tests/chaos -q --durations=10` |
| R9 | litellm attribute drift: if `received_finish_reason` is renamed, **every** stream reports truncated. Verified present in the pinned litellm and re-measured in this worktree. | `uv run pytest -q tests/chaos/test_chaos_gateway_faults.py -k healthy` fails loudly |
| R10 | The counter semantics change is user-visible ("Cloud calls: N" now counts attempts, an upper bound on egress). | `uv run pytest -q tests/redteam tests/exploratory/test_explore_egress_counter.py tests/integration/tui` |
| R11 | `_call_with_fallback`'s new keyword-only params silently break any *unlisted* fake (a signature mismatch surfaces as `TypeError: ... unexpected keyword argument`). | `uv run pytest -m "fast or slow" -q`; grep `-rn "_call_with_fallback" tests/` |
| R12 | `test_gateway_router.py::TestChat::test_falls_back_to_fallback_model` relies on that module's autouse `_mark_pii_available` fixture; after T2 the *fallback* gate also consults the PII flag. A new test written outside that file (or in a file that resets the flag) will fail with `PIIUnavailableError`. | `uv run pytest -q tests/unit/test_gateway_router.py::TestChat -p randomly` |
| R13 | `_record_cloud_call` now runs **before** the dispatch, so a bookkeeping exception aborts the call. It only calls `load_registry()` (already called by the gate on the same path) and mutates two integers; no test covers it either way. | Design §7.3 — decide in review; `uv run pytest -q tests/chaos` |
| R14 | D2 is custom-provider-scoped (Documented change 4): for bundled providers the `api_key` pop is inert, so a future regression in that pop would only be caught by the custom-primary anchor. | `uv run pytest -q tests/unit/test_gateway_router.py::TestChat::test_fallback_dispatch_carries_the_fallback_providers_api_base_and_drops_the_primarys_key` |

---

## Deviations from the design doc (summary)

1. **No `EmptyResponseError`, no `TruncatedStreamError`** (lead adjudication A1).
   `UnclassifiedProviderError` and the existing `gateway.errors.ConnectionError` are reused; the
   provider name lives in the message. Consequence recorded in R7 (recovery routing changes) and R6
   (truncation shares the timeout class).
2. **`redact_text(text, patterns=...)`, no `register_secret`** (A3, D1). The design's §3.3
   registration machinery is cut; the residual gap is Documented change 1. `_apply_provider_credentials`
   is untouched (`field.secret` becomes irrelevant to this change).
3. **No `_provider_kwargs_for`** (A2) and **no `_switch_to_fallback` / `_retarget_provider_kwargs`**
   names: the gate + retarget are inlined into the fallback branch of `_call_with_fallback`. The
   primary's `api_base`/`api_key`/declared credential kwargs are dropped because that is a concrete
   custom-provider credential-to-the-wrong-host defect (D2), not a refactor. A custom-provider
   fallback is left fail-closed (R4).
4. **`chat_stream` owns the cost row and `done`; `_iter_stream` keeps its two-parameter signature**
   (D3), instead of the design's `_iter_stream`-owns-both sketch. `_iter_stream`'s terminal check sits
   **after** its `try/except`, so no `isinstance(exc, GatewayError)` guard is needed (audit).
5. **Single terminal predicate** (D4): `bool(getattr(response, "received_finish_reason", None))` —
   `intermittent_finish_reason` is never consulted. The test double carries only the one attribute, and
   puts `finish_reason` on the **choice** object.
6. **`_is_permanent_error` and `_require_content` are inlined** at their single call sites (T8, T4); no
   extra method names or docstrings.
7. **One shared stream double** in `tests/helpers/stream_doubles.py`, imported by
   `tests/redteam/_w5_probe.py` and by the unit stream tests.
8. **Ownership split (blocking finding 1):** `chat_stream`'s dispatch, its `router.py:674` counter, the
   `setdefault("timeout", …)` fix and the `_iter_stream` classification are **T5's alone**; T1 touches
   only `chat` (`:630`) and `embed` (`:746`), T7 only `rerank` (`:808`). No line is edited by two tasks.
