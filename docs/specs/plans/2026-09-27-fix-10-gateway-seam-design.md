# Gateway seam — design for 10 open issues

**Scope:** #143, #144, #146, #147, #149, #150, #151, #152, #153, #154 — one seam:
`src/openreview_cli/gateway/router.py` (`Gateway`), plus `gateway/redaction.py`,
`gateway/errors.py` (both in scope for new names), `app.py`'s `gateway test` echo, and the
test harnesses named in §5.

**Base:** worktree `gateway-seam`, clean at `1b6c6d4` (origin/main).

**Baseline measured (not assumed):**

```
uv run pytest --runxfail -q -p no:randomly --no-header <the 10 named node ids>
=> 12 failed, 2 passed
```

The two that **pass** today are the characterisation pins, not the expectations:
`tests/chaos/test_chaos_accounting.py::test_no_choice_reply_leaves_a_cost_row_while_the_call_fails`
and `...::test_rerank_has_no_retry_loop`. Both must be **inverted** (§4.2), not un-marked.
The other 12 cases are `xfail(strict=True)` and go red under `--runxfail`.

**Deliverable of this doc:** the per-issue design + the exhaustive list of existing tests that pin
the old behaviour. Every claim below was checked against the code and, where marked *(measured)*,
against a throwaway probe run under the worktree's venv.

---

## 1. Problem statement per issue

| # | Problem (1–2 lines) | Exact `router.py` site that causes it |
|---|---|---|
| 143 | The cloud-call counter fails **open** for an unclassifiable provider (`base_url=None, is_local=False`, e.g. bundled `bedrock`/`vertex`) while the tier gate fails **closed** on the same state; a dispatched call is therefore invisible to the privacy footer. | `_record_cloud_call` `:835-840` (`except ValueError: return`) vs `_enforce_tier` `:286-290` / `:326-332` (`klass = "cloud"`). |
| 144 | The config-level `gateway.models.<slot>.fallback` model is dispatched with **no tier evaluation** and inherits the primary's `api_base`. | `_call_with_fallback` `:546-558` — `call_kwargs["model"] = fallback` (`:553`), dispatch (`:555`); the only gate runs once in `chat`/`chat_stream` (`:622`, `:657`). |
| 146 | `gateway test` echoes an upstream error body verbatim, so a provider/proxy that reflects the request credential prints the API key to **stderr**; the same text on the *log* path is already redacted. | `_classify_error` `:464-521` (`AuthError(provider, str(exc))` at `:509`) → `app.py:1702` `typer.echo(f"Error: {e}", err=True)`. |
| 147 | The counter counts **logical calls**, not dispatches: `retries+1` attempts and a fallback model under-report egress. | `_call_with_fallback` loop `:538-544` + fallback `:546-558`, while each public method calls `_record_cloud_call` exactly once *after* the call (`:630`, `:674`, `:746`, `:808`). |
| 149 | A reply with no usable choice writes a `cost_logs` row, then crashes with a raw `AttributeError`/`IndexError` instead of a typed gateway error. | `chat` `:634-639` (`log_call`, deliberately non-fatal) runs **before** `:640` `response.choices[0].message.content`. |
| 150 | A `cost_logs` row is written for a call whose reply was never received — `chat` (above) and `chat_stream` (logs **before** `_iter_stream` drains at `:685`). | `chat` `:634-639`; `chat_stream` `:679-684` vs `yield from self._iter_stream(...)` `:685`. |
| 151 | `chat_stream` bypasses `_call_with_fallback` and disables retries, so a litellm exception escapes raw (typed errors are the contract everywhere else). | `chat_stream` `:664` (`num_retries = 0`), `:672` (`response = completion(**call_kwargs)` direct); `_iter_stream`'s bare `raise` `:717`. |
| 152 | An unterminated SSE stream is reported as completed — `done` is emitted whenever the chunk loop ends. | `_iter_stream` `:687-693`: unconditional `yield StreamingOutputEvent(type="done")` at `:693`. |
| 153 | `rerank` has no retry loop (own `try/except`), so `gateway.fallback.retries` is not applied. | `rerank` `:790-800` (single `rerank(...)` call, `except` → classify), instead of `_call_with_fallback`. |
| 154 | Permanent classes (401/404) are retried `retries + 1` times. | `_call_with_fallback` `:538-544` — `except Exception` with no class filter. |

---

## 2. Contract decisions (settled here, they drive everything below)

- **C1 — the counter counts every dispatch attempt, including failed ones.** #143 and #147 are one
  fix with one arithmetic: `_cloud_calls_made` / `get_total_cloud_calls()` increment **immediately
  before** each `call_fn(**call_kwargs)` that reaches the provider SDK, on every attempt of the
  retry loop and on the fallback dispatch. Consequence (documented, honest): a run whose three
  attempts all fail reports `Cloud calls: 3`, because the process cannot distinguish an attempt that
  died before the bytes left from one that answered with a 500. The counter is therefore an **upper
  bound** on egress, never an under-report — which is the direction the privacy footer needs.
- **C2 — #149's "no counter increment" clause is superseded by C1.** A no-choice reply *was*
  dispatched (a 200 came back; cost may have been incurred), so it counts. The committed probe agrees:
  `test_chaos_gateway_faults.py::test_no_choice_response_logs_a_cost_and_then_crashes_raw` asserts
  `gw._cloud_calls_made == 1` ("the counter did count the dispatch"), and the `xfail` expectation node
  `...::test_no_choice_response_is_a_typed_error_and_logs_no_cost` asserts only *typed error* and
  *no cost row* — it is silent on the counter. #149's real defect is the cost row and the raw crash;
  both are fixed here. What #149 gets from C1: no cost row, a typed error, and **one** count (not
  zero). Record this arbitration in the PR description.
- **C3 — retries and fallback apply only before the first chunk reaches the caller.** A stream that
  fails during iteration has already emitted text; re-dispatching would duplicate or interleave
  output. So `chat_stream` retries/falls back on the **dispatch** (safe: nothing yielded yet) and
  never after the first `yield`.
- **C4 — the stream terminal marker is litellm's own "the provider sent a finish_reason" state**, not
  `chunk.choices[0].finish_reason` (which litellm fabricates). See §6 for the measurement.
- **C5 — a tier-blocked fallback propagates the typed tier error** (`NoMatchingProviderError` /
  `PIIUnavailableError`), it does not fall back to "re-raise the primary's error". Blocking a
  fallback is a security decision; hiding it inside `UnclassifiedProviderError` would make a
  privacy-tier refusal look like a transport blip.
- **C6 — permanent = `AuthError | ModelNotFoundError`** (the two classes `_classify_error` already
  produces for 401/403/auth and 404/not-found). Permanent stops the **same-model** retry loop after
  one dispatch; a configured fallback model is still tried **once** (a different provider may accept
  the same credential). The `#154` node has no fallback configured, so `attempts == 1` holds.
- **C7 — jitter / total-time cap / 400-as-permanent are explicitly *not* done.** #154 lists them as
  "consider"; doing them would invalidate the green pins
  `test_retry_delay_is_fixed_with_no_jitter_or_cap` and `test_retry_shape_is_three_attempts_for_every_fault_class`'s
  transient rows for no repro node. Out of scope.

---

## 3. Design per issue (function level, order of operations)

### 3.1 #143 + #147 — one fix: count inside `_call_with_fallback`

**New signature** (the two new keyword-only params are the only signature change in the seam):

```python
def _call_with_fallback(
    self,
    slot: str,
    call_fn: Any,
    call_kwargs: dict[str, Any],
    *,
    call_type: str = "llm",              # "llm" | "embedding" | "reranking"
    provider_prefix: str | None = None,  # the recovery-driven `model=` override's prefix
) -> Any:
```

Order of operations:

```python
    cfg = self._get_slot_config(slot)
    primary = cfg["primary"]
    provider = provider_prefix or primary.split("/", 1)[0]
    fallback_cfg = self._config.get("gateway", {}).get("fallback", {})
    retries, retry_delay, timeout = ...
    call_kwargs.setdefault("timeout", timeout)      # was: unconditional assignment (see §3.5)

    last_error = None
    for attempt in range(retries + 1):
        self._record_cloud_call(slot, provider_prefix=provider)   # <-- count the ATTEMPT
        try:
            return call_fn(**call_kwargs)
        except Exception as e:
            last_error = e
            if self._is_permanent_error(e, provider):             # #154
                break
            if attempt < retries:
                time.sleep(retry_delay)

    fallback = cfg.get("fallback")
    if slot in PRIMARY_ONLY_SLOTS or not fallback:
        if last_error is not None:
            raise self._classify_error(last_error, provider) from last_error
        raise AllProvidersFailedError("All providers failed")

    fallback_prefix = self._switch_to_fallback(slot, call_type, fallback, call_kwargs)  # #144
    self._record_cloud_call(slot, provider_prefix=fallback_prefix)                       # #147
    try:
        return call_fn(**call_kwargs)
    except Exception as e:
        raise self._classify_error(e, provider) from e
```

- The count happens **before** the `try`, so a failed attempt is counted (#147's probe asserts
  `_cloud_calls_made == len(flaky.records) == 3` where attempts 1–2 raise).
- Unclassifiable providers (#143): `_record_cloud_call` keeps its **name and signature** (it is
  monkeypatched on instances by `tests/unit/test_gateway_router.py:909,1058`,
  `tests/unit/test_gateway_tier_enforcement.py:277,297`, `tests/integration/test_gateway_streaming.py:53`,
  `tests/exploratory/test_explore_egress_counter.py:354-383`) but its `ValueError` branch flips from
  fail-open to fail-closed — the same verdict `_enforce_tier` already reaches:

```python
        try:
            klass = classify_provider(info)
        except ValueError:
            # One state, one resolution (R-01/#143): _enforce_tier treats an
            # unclassifiable provider as cloud, so the counter must too.
            klass = "cloud"
        if klass == "cloud":
            self._cloud_calls_made += 1
            record_cloud_call()
```
- Call sites (each keeps its own gate at the top; none calls `_record_cloud_call` any more):
  - `chat`: `self._call_with_fallback(slot, completion, call_kwargs, provider_prefix=override_prefix)`
  - `chat_stream`: same (plus `call_type` default `"llm"`)
  - `embed`: `..., call_type="embedding"`
  - `rerank`: `..., call_type="reranking"`
- The four post-dispatch `_record_cloud_call` calls (`:630`, `:674`, `:746`, `:808`) are **deleted** —
  they structurally cannot count a call that failed.
- Custom-provider note: the counter prefix comes from the **declared** model
  (`provider_prefix or cfg["primary"].split("/")[0]`), never from the litellm-rewritten
  `call_kwargs["model"]` (`_get_litellm_kwargs` rewrites `lmstudio/x` → `openai/x` at `:410-413`), so a
  local custom provider keeps counting 0.

### 3.2 #144 — the fallback gets a gate and a re-derived provider target

```python
    def _switch_to_fallback(
        self, slot: str, call_type: str, fallback_model: str, call_kwargs: dict[str, Any]
    ) -> str:
        """Gate and retarget the model that will actually hit the network."""
        fallback_prefix = fallback_model.split("/", 1)[0]
        # Same gate as the primary, against the ACTUAL model (router.py:243-244).
        self._enforce_tier(slot, call_type, provider_prefix=fallback_prefix)
        self._retarget_provider_kwargs(slot, fallback_model, call_kwargs)
        call_kwargs["model"] = fallback_model
        return fallback_prefix
```

`_retarget_provider_kwargs` must undo the primary's provider block and apply the fallback's. To avoid
a second copy of that block, extract `_get_litellm_kwargs`'s provider section (`:399-419`) verbatim
into one helper reused by both paths:

```python
    def _provider_kwargs_for(self, model: str, info: ProviderInfo | None) -> dict[str, Any]:
        """api_base / credential kwargs / custom-provider rewrite for *model*."""
```
- `_get_litellm_kwargs` keeps its exact behaviour: it calls the helper with `cfg["primary"]` and the
  slot's `ProviderInfo` (`kw["model"] == "openai/some-model"`, `api_base`, `api_key` for custom
  providers — pinned by `tests/unit/test_gateway_router.py:1102-1169`).
- The fallback path pops every key the primary's block contributed, then updates with the fallback's
  block:

```python
        for key in self._provider_kwargs_for(cfg["primary"], self._resolve_provider_info(slot)):
            call_kwargs.pop(key, None)
        call_kwargs.pop("api_base", None)
        call_kwargs["model"] = fallback_model
        call_kwargs.update(self._provider_kwargs_for(fallback_model, registry.get(fallback_prefix)))
```
- Deliberately **not** applied to the `model=` override path: that is RT-032
  (`test_a_tier_approved_local_override_and_the_destination_disagree`, green today,
  `test_a_tier_approved_local_override_is_not_sent_to_a_cloud_host`, still `xfail`) and is **out of
  scope** for these 10 issues. Keeping the retargeting inside the fallback branch is what keeps RT-032
  out.
- `_enforce_tier(slot, call_type, provider_prefix=...)` already implements everything needed for an
  unknown or unclassifiable fallback prefix (fail closed at `:252-282`), so nothing new is written in
  the gate.

### 3.3 #146 — redaction reaches the CLI echo

`src/openreview_cli/gateway/redaction.py`:

```python
_registered_secrets: set[str] = set()
_MIN_SECRET_LEN = 8

def register_secret(value: str | None) -> None:
    """Remember a resolved credential so redact_text masks it even when it has no `sk-` shape."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _registered_secrets.add(value)

def redact_text(text: str) -> str:
    """Redact credential-shaped material from any text bound for a user surface.

    Order mirrors RedactingFilter._redact: the value passes first (a literal "sk-" pass
    would otherwise mask the prefix and leave the key body), literal patterns last.
    """
    text = _KEY_VALUE_RE.sub(lambda m: redact_key(m.group(0)), text)
    for secret in _registered_secrets:
        if secret in text:
            text = text.replace(secret, redact_key(secret))
    for pat in REDACT_PATTERNS:
        if pat in text:
            text = text.replace(pat, redact_key(pat))
    return text
```
and `RedactingFilter._redact` becomes a call to `redact_text` (one implementation for both surfaces,
so registered secrets also cover log records).

Registration points (the credential-resolution seams the issue names):
- `_set_env_vars` (`:176-190`): `register_secret(creds)` for every `str` entry of `auth.json`, and
  `register_secret(val)` for every value of a dict entry — **independently of** whether the env var
  was already set, because the value is a credential either way. This is the load-bearing point for
  bundled single-key providers: the real registry's `openai` entry declares **no** `credentials`, so
  `_apply_provider_credentials` never runs for it (verified in
  `src/openreview_cli/gateway/models.json`).
- `_apply_provider_credentials` (`:363-377`): `if field.secret: register_secret(value)`
  (`CredentialField.secret` already exists) — for multi-field providers (bedrock/vertex).
- `_get_litellm_kwargs`'s custom-provider key injection (`:414-418`): `register_secret(key)`.

Application (so the classified message is already clean wherever it is printed, not only in one
command):
- `_classify_error` builds every typed error from `redact_text(str(exc))` instead of `str(exc)`
  (one local, e.g. `detail = redact_text(str(exc))`, used at `:509`, `:511`, `:513`, `:517`, `:521`).
- `app.py:1702` (`gateway test`) becomes `typer.echo(f"Error: {redact_text(str(e))}", err=True)` — this
  is the surface the probe measures, and it also covers an unclassified escape that does **not** pass
  through `_classify_error` (§3.5's mid-stream path re-raises a *classified* error, but a future raw
  path must not leak either).
- Recommended same-commit hardening: `_format_exception` (`app.py:97-106`) also returns
  `redact_text(...)`; it is the shared formatter for ~6 other command echoes and costs one line. It
  does **not** cover `gateway test` (which formats its own line), hence both changes.

Why this is enough for the probe: the canary `sk-test-CANARY-123` matches `_KEY_VALUE_RE`
(`sk-` + 15 chars of `[A-Za-z0-9_-]`, `{12,}`) so it is masked by `redact_key` → `sk-t***********`;
`register_secret` is the defence for a key that does *not* have a `sk-`/`pk-`/`rk-` shape.

### 3.4 #149 + #150 — decide "usable reply" before booking cost

New private guard, called by `chat` **before** `log_call`:

```python
    def _require_content(self, response: Any, provider: str) -> str:
        """Return the reply text, or raise EmptyResponseError when no usable choice exists."""
        choices = getattr(response, "choices", None)
        if not choices:
            raise EmptyResponseError(provider, "reply carried no choice")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise EmptyResponseError(provider, "reply choice carried no message")
        return getattr(message, "content", None) or ""
```
- `response is None` (the W0 `delay` fault), `choices == []` (content-filtered / tool-only shape,
  `_w7.EmptyChoicesResponse`) and a missing `message` all map to `EmptyResponseError` naming the
  provider; `content=None` still returns `""` as today.
- `provider` is `override_prefix or provider_prefix` so the message names the model that was
  actually dispatched.
- New class in `errors.py`, shaped like `AuthError` (`provider` + `message` attributes):

```python
class EmptyResponseError(GatewayError):
    def __init__(self, provider: str, message: str = "") -> None:
        self.provider = provider
        self.message = message
        super().__init__(f"empty response from {provider}: {message}")
```
  (Minimal alternative — reuse `UnclassifiedProviderError` with a `[provider]`-prefixed message — was
  rejected: it has no `.provider` attribute and the recovery layer deliberately routes it as
  *transient → provider_fallback*, whereas a reply with no choice is not worth retrying on another
  provider. The new class lands in the recovery layer's `unknown` category → `user_guided_recovery`.
  See §7 risk 4.)

`chat`'s new order (the only reordering in the method):

```python
        response = self._call_with_fallback(slot, completion, call_kwargs, provider_prefix=override_prefix)
        content = self._require_content(response, override_prefix or provider_prefix)   # NEW
        try:                                                    # cost logging stays non-fatal (T030)
            self._cost_tracker.log_call(session_id, slot, call_kwargs["model"], provider_prefix, response)
        except Exception as cost_err:
            logger.warning("Cost logging failed (non-fatal): %s", cost_err)
        return content
```

`chat_stream`'s cost row moves to the **post-terminal** point, inside the generator (§3.6): the row
is written only after the terminal marker is seen, so a truncated / never-yielding stream leaves
none. `embed`/`rerank` keep log-after-response; `rerank`'s `response.results` deref (`:809-811`)
stays after the row on the same "reply was received" argument — no repro node covers a `results`-less
rerank reply, so it is left alone (noted in §7).

### 3.5 #151 — the stream joins the retry/classification policy

Measured facts that shape this (probe #4, §6.4):

| failure | when it surfaces |
|---|---|
| provider down / connection refused | **eagerly** — `completion(stream=True)` itself raises `litellm.InternalServerError` |
| malformed SSE body | **lazily** — `completion()` returns a wrapper; the first `next()` raises `litellm.MidStreamFallbackError` |

So the fix is two-sided:

1. **Dispatch**: `chat_stream` replaces `response = completion(**call_kwargs)` with
   `response = self._call_with_fallback(slot, completion, call_kwargs, provider_prefix=override_prefix)`.
   `num_retries = 0` stays (litellm must not retry underneath; the gateway owns the retry layer).
   **Yes, retries apply to streaming** — at dispatch time no chunk has been yielded, so it is as safe
   as `chat`; and the fallback model is tried there too for the same reason.
   Requires `_call_with_fallback` to stop clobbering the streaming timeout:
   `call_kwargs["timeout"] = timeout` (`:535`) becomes `setdefault`, otherwise the dual
   `httpx.Timeout(15, 45)` set at `:665-670` is replaced by an int and
   `test_stream_timeout_is_dual_not_single` / `test_stream_idle_timeout_cuts_stalled_provider` break.
   `chat`/`embed`/`rerank` pass no `timeout`, so their behaviour is unchanged.
2. **Mid-stream**: `_iter_stream`'s final `raise` (`:717`) becomes a classification, so a lazy
   mid-stream failure is typed too. Order inside the existing `except Exception as exc`:
   `if isinstance(exc, GatewayError): raise` → the two `httpx` timeout branches (unchanged) → the
   litellm-timeout heuristic (unchanged) → `raise self._classify_error(exc, provider_prefix) from exc`.

### 3.6 #152 — the terminal marker

`_iter_stream` gains the terminal check between the loop and `done`:

```python
        for chunk in response:
            delta = getattr(chunk.choices[0].delta, "content", None) if chunk.choices else None
            if delta:
                yield StreamingOutputEvent(type="chunk", text=delta)
        if not _stream_terminated(response):                    # NEW  — see §6
            raise TruncatedStreamError(
                provider_prefix,
                "stream ended without a terminal finish_reason (truncated or interrupted)",
            )
        <cost log for the stream, only now>                     # #150
        yield StreamingOutputEvent(type="done")
```

with

```python
def _stream_terminated(response: Any) -> bool:
    """True when the provider itself signalled a finish_reason.

    litellm's CustomStreamWrapper fabricates ``finish_reason="stop"`` on EOF
    (litellm_core_utils/streaming_handler.py:1875-1888, reached from the
    StopIteration branch of __next__), so the CHUNK's finish_reason cannot be
    used. ``received_finish_reason``/``intermittent_finish_reason`` are set only
    from a provider-sent marker — the same pair litellm itself consults to
    decide whether to fabricate one.
    """
    return bool(
        getattr(response, "received_finish_reason", None)
        or getattr(response, "intermittent_finish_reason", None)
    )
```

New error:

```python
class TruncatedStreamError(GatewayError):
    def __init__(self, provider: str, message: str = "") -> None:
        self.provider = provider
        self.message = message
        super().__init__(f"incomplete stream from {provider}: {message}")
```

Raising (rather than yielding `StreamingOutputEvent(type="error")`) keeps one failure channel:
timeouts already raise `ConnectionError` out of the same method, and the W7b oracle
(`_w7.StreamOutcome.completed`) treats "caught a typed error naming the provider" as an honest
degradation. `chat_stream` is consumed nowhere in `src/` today (grep: only `router.py`), so the blast
radius is the test suites.

### 3.7 #153 — `rerank` goes through the same policy

```python
        from litellm import rerank                       # keep the local import (tests patch litellm.rerank)

        call_kwargs: dict[str, Any] = {
            "query": query, "documents": documents, "top_n": top_n,
            **self._get_litellm_kwargs(slot),
        }
        response = self._call_with_fallback(slot, rerank, call_kwargs, call_type="reranking")
        try:
            self._cost_tracker.log_call(
                session_id, slot, call_kwargs["model"], cfg["primary"].split("/")[0], response
            )
        except Exception as cost_err:
            logger.warning("Cost logging failed (non-fatal): %s", cost_err)
        return [{"index": r["index"], "relevance_score": r["relevance_score"]} for r in response.results]
```
- `timeout` comes from `setdefault("timeout", cfg gateway.fallback.timeout)` — same value as today's
  explicit `timeout=timeout` (`:795`), so no behaviour change there.
- `cfg["primary"].split("/")[0]` stays the provider named in the `cost_logs` row and in errors
  (`test_rerank_error_names_provider` asserts `provider == "cohere"`).
- `reranking` is in `PRIMARY_ONLY_SLOTS`, so the fallback branch stays unreachable for rerank while
  retries now apply — exactly what #153 asks for.

### 3.8 #154 — permanent errors short-circuit

```python
    def _is_permanent_error(self, exc: Exception, provider: str) -> bool:
        """A retry cannot fix a bad credential or a missing model."""
        return isinstance(self._classify_error(exc, provider), (AuthError, ModelNotFoundError))
```
- Called inside the loop after the failed attempt (so the one attempt is still counted, C1) and
  before the `time.sleep`; on `True` the loop `break`s (C6: fallback still available once).
- Reuses `_classify_error`, so 401/403/"invalid api key"/"unauthorized" and 404/"not found" are
  permanent with no second classification table. W7's injected `AuthFaultError`/`ModelNotFoundFaultError`
  (`status_code = 401/404`) hit the existing `status == 401` / `status == 404` branches.

### 3.9 Order of operations — public methods after the change

| method | order |
|---|---|
| `chat` | slot check → override prefix → `_enforce_tier("llm")` → `_prepare_chat` → kwargs → `_call_with_fallback` (count+dispatch per attempt, gate+retarget+count+dispatch on fallback) → `_require_content` → `log_call` (non-fatal) → return text |
| `chat_stream` | same gate → `_prepare_chat` → kwargs (`num_retries=0`, dual `httpx.Timeout`) → `_call_with_fallback` → iterate (chunk events) → classify mid-stream escape → terminal check (`TruncatedStreamError`) → `log_call` (non-fatal) → `done` |
| `embed` | gate → capability → cost limits → prompt → kwargs → `_call_with_fallback(call_type="embedding")` → `log_call` → `response.data` |
| `rerank` | gate → capability → cost limits → prompt → kwargs → `_call_with_fallback(call_type="reranking")` → `log_call` → `response.results` |

---

## 4. Existing tests that pin the OLD behaviour

### 4.1 `xfail(strict=True)` probes — remove the marker, assertions unchanged

| node | where |
|---|---|
| `tests/redteam/test_redteam_egress_guard.py::test_unclassifiable_provider_dispatch_increments_the_cloud_counter` | `:316` |
| `...::test_retry_dispatches_increment_the_counter_once_each` | `:447` |
| `...::test_a_cloud_fallback_model_never_reaches_dispatch_on_the_strict_tier` | `:493` |
| `tests/redteam/test_redteam_gateway_setup.py::test_gateway_test_does_not_echo_key_material_from_a_provider_error` | `:251` |
| `tests/chaos/test_chaos_gateway_faults.py::test_no_choice_response_is_a_typed_error_and_logs_no_cost` (2 params) | `:155` |
| `...::test_stream_dispatch_failure_is_a_typed_gateway_error` (2 params) | `:292` |
| `...::test_stream_without_a_completion_marker_is_not_reported_as_done` (2 params) | `:333` |
| `tests/chaos/test_chaos_accounting.py::test_permanent_errors_are_not_retried` (2 params) | `:172` |

### 4.2 Characterisation pins that must be INVERTED (they pass today)

| file · test | exact assertion that changes |
|---|---|
| `tests/redteam/test_redteam_egress_guard.py` · `test_unclassifiable_provider_dispatch_is_not_counted` (`:291`) | `assert gw._cloud_calls_made == 0` → `== 3`; `assert get_total_cloud_calls() == 0` → `== 3`; rename (`..._is_counted`) + docstring ("same state, two resolutions" is now one resolution). |
| same file · `test_counter_inequality_rows_are_measured` (`:383`) | Expected rows become `[("chat, 2 retries", 3, 3), ("chat, fallback model", 3, 0), ("chat, unknown slot primary", 0, 0), ("chat, unclassifiable provider", 3, 3)]`. Two extra edits: `assert gw.chat(...) == "ok"` (`:415`) → `pytest.raises(NoMatchingProviderError)` (the fallback is tier-blocked, so the row is 3 dispatches, all local, not 4); row labels/docstring lose the "inequality" framing. |
| same file · `test_fallback_model_is_dispatched_on_the_strict_tier` (`:465`) | Invert: `pytest.raises(NoMatchingProviderError)` around `gw.chat(...)`; `runner.models() == ["ollama/qwen3:8b"] * 3` (no `anthropic/claude-3-5-haiku`); `runner.records[-1].api_base == OLLAMA_BASE_URL` still holds for the last **local** attempt; `gw._cloud_calls_made == 0` still holds (all three dispatches are local). Rename. |
| `tests/chaos/test_chaos_gateway_faults.py` · `test_no_choice_response_logs_a_cost_and_then_crashes_raw` (`:170`) | Invert: `pytest.raises(EmptyResponseError)` (not `(AttributeError, IndexError)`), `assert not isinstance(...)` line removed; `rows == []` (was `len(rows) == 1` with `EXPECTED_NO_CHOICE_COST`); keep `flaky.gw._cloud_calls_made == 1` (C2). |
| same file · `test_in_process_fault_is_a_typed_gateway_error` (`:88`) | `assert flaky.gw._cloud_calls_made == 0` (`:102`) → `== flaky.seam.attempts`; `assert get_total_cloud_calls() == 0` (`:103`) → `== flaky.seam.attempts`; keep the `cost_rows == []` line. |
| same file · `test_stream_dispatch_failure_escapes_as_a_raw_litellm_error` (`:307`) | Invert: `assert isinstance(outcome.caught, GatewayError)` (drop `not isinstance` and the `type(...).__module__.startswith("litellm")` line); keep `_cloud_calls_made == 0` (the W7b registry declares the loopback provider `is_local=True`, so dispatch counts nothing) and `cost_rows == []`; rename (`..._is_a_typed_gateway_error_and_logs_no_cost`). |
| same file · `test_stream_without_a_completion_marker_is_reported_as_done_today` (`:353`) | Invert: `not outcome.completed`; `isinstance(outcome.caught, TruncatedStreamError)` + provider name in the message; `assert _w7.cost_rows(gw._data_path) == []` (was `len(rows) == 1`); keep `_cloud_calls_made == 0`. Rename. |
| `tests/chaos/test_chaos_accounting.py` · `assert_no_earned_cost` helper (`:64`) + `test_faulted_call_logs_no_cost_and_earns_no_counter` (`:75`) | Two assertions change. (a) `assert flaky.seam.attempts == EXPECTED_RETRY_ATTEMPTS` (`:84`) is parametrised over `NO_COST_FAULTS` which includes the permanent `auth`/`not_found`: it becomes 3 for `timeout`/`rate_limit`/`five_hundred`/`five_hundred_three` and **1** for `auth`/`not_found` (split the table or compare against a per-fault expectation). (b) Helper: add an `attempts` parameter and assert `gw._cloud_calls_made == attempts` and `process_calls == attempts` (was `== 0`); keep `rows == []`. Caller passes `flaky.seam.attempts`; rename helper/test (`..._counts_only_the_dispatches_that_happened`). Negative control `test_negative_control_accounting_oracle_rejects_a_real_cost_row` (`:418`) updates its call (its `rows != []` trigger still fires first). |
| same file · `test_retry_then_success_logs_one_row_for_three_dispatches` (`:110`) | `assert flaky.gw._cloud_calls_made == 1, "one logical call earned exactly one increment"` (`:132`) → `== 3`; `assert get_total_cloud_calls() == 1` (`:133`) → `== 3`; docstring/comment must stop calling the retry asymmetry "RT-028 … not re-opened here" — the ledger cross-check `len(rows) == 1` stays, the counter no longer equals it. |
| same file · `test_retry_shape_is_three_attempts_for_every_fault_class` (`:157`) | `assert recorded == dict.fromkeys(NO_COST_FAULTS, EXPECTED_RETRY_ATTEMPTS)` → transient faults (`timeout`, `rate_limit`, `five_hundred`, `five_hundred_three`) `== 3`, permanent (`auth`, `not_found`) `== 1`; docstring "every exception class — permanent ones included — is retried" must be rewritten. |
| same file · `test_permanent_errors_are_retried_today` (`:185`) | Invert: `assert flaky.seam.attempts == 1` (was `EXPECTED_RETRY_ATTEMPTS`); keep the `isinstance(exc, GatewayError)` line. Rename. |
| same file · `test_rerank_has_no_retry_loop` (`:393`) | Invert: `assert len(calls) == 3` (was `1`); `assert flaky.gw._cloud_calls_made == 3` (was `0`); keep "openai" in the error and `cost_rows == []`. Rename (`test_rerank_uses_the_configured_retry_loop`). |
| same file · `test_chat_stream_disables_retries_and_never_tries_the_fallback` (`:369`) | Invert: `flaky.seam.attempts == 4` (was `1`); `flaky.seam.models == ["openai/gpt-4o"] * 3 + ["anthropic/claude-3-5-haiku"]`; `isinstance(outcome.caught, GatewayError)` (drop the `type(...).__name__ == "ConnectionError"` and `not isinstance` lines); `_cloud_calls_made == 4` (both providers are cloud in that registry — `anthropic` is not in `_w7.cloud_registry()`, so it takes the fail-closed unknown-prefix count); `cost_rows == []` stays. Rename. |
| same file · `test_no_choice_reply_leaves_a_cost_row_while_the_call_fails` (`:136`) | Invert: `pytest.raises(EmptyResponseError)` (was `AttributeError`); `assert _w7.cost_rows(...) == []` (was `len(rows) == 1`); add `assert flaky.gw._cloud_calls_made == 1` (C2). The RT-039 evidence file must be superseded in the PR description. |
| `tests/exploratory/test_explore_egress_counter.py` · `test_record_cloud_call_classifies_local_vs_cloud_slots` (`:333`) | `assert gateway._cloud_calls_made == 2, "resolution error must not coerce to cloud"` (`:375`) → `== 3` with a message saying the unclassifiable provider now resolves as cloud (same verdict as the gate); `assert ... == 3, "unknown override counts as cloud"` (`:379`) → `== 4`; the known-local-override assertion (`:383`) `== 3` → `== 4`. |
| `tests/unit/test_gateway_tier_enforcement.py` · `test_cloud_call_counter_uses_override_prefix` (`:723`) | The counter moved inside `_call_with_fallback`, so `monkeypatch.setattr(gw, "_call_with_fallback", lambda *a, **k: _chat_response("ok"))` (`:749`) bypasses it and `gw.privacy_report().cloud_calls_made == 1` (`:761`) becomes 0. Patch the real dispatch seam instead (`router_mod.completion = lambda **kw: _chat_response("ok")`) and keep `== 1`. |
| `tests/unit/test_gateway_router.py` · `test_chat_stream_yields_chunks_and_done` (`:977`) | The `completion` fake returns `iter(chunks)` (a bare list iterator with no terminal state) → after the fix the stream raises `TruncatedStreamError` instead of yielding `done`. Update the fake to the terminated-stream double (§5.2); the event-list assertion stays. |
| same file · `test_chat_stream_survives_cost_logging_failure` (`:953`) | Same fake change; the event list stays; the `log_call` raise is now on the post-terminal path, so keep the "survives" assertion and add that `done` is still yielded. |
| same file · `test_stream_timeout_is_dual_not_single` (`:995`) | `fake_completion` returns `iter([])` → `list(gw.chat_stream(...))` now raises. Wrap in `pytest.raises(TruncatedStreamError)` (documents the empty-stream contract) and keep both timeout assertions. |

### 4.3 Fakes that break on the new `_call_with_fallback` signature (keyword-only additions)

Every replacement must accept the new kwargs (mechanically: `def _fake_fallback(slot, call_fn, call_kwargs, **_kw)`).

| file · test | line |
|---|---|
| `tests/unit/test_gateway_router.py` · `TestEmbed::test_survives_cost_logging_failure` | `:243` |
| ... · `test_strip_in_chat_path_integrates` | `:835` |
| ... · `test_strip_all_empty_raises_empty_messages` | `:913` |
| `tests/unit/test_gateway_tier_enforcement.py` · `TestBalancedTier::test_local_embed_proceeds` | `:122` |
| ... · `TestBalancedTier::test_cloud_embed_allowed` | `:180` |
| ... · `TestR35RecoveryTierBypass::test_balanced_allows_legal_cloud_model_override` | `:382` |
| ... · `...::test_chat_with_model_override_uses_override_for_tier_check` | `:450` |
| ... · `...::test_tier_matrix` | `:596` |
| `tests/unit/test_r35_recovery_seam.py` · `test_balanced_same_provider_override_allowed` (class-level patch) | `:202` |

Unaffected: `_assert_dispatch_not_reached(*args, **kw)`, the `lambda *a, **k: ...` patches, and
`patch.object(Gateway, "_call_with_fallback", return_value=...)` in
`tests/integration/test_prompt_gateway.py:80`.

### 4.4 Verified GREEN after the change (no edit — recorded so the Plan stage can skip them)

- `tests/redteam/test_redteam_egress_guard.py`: all strict-tier block tests
  (`test_gateway_test_blocks_cloud_dispatch_on_the_strict_tier`, `test_precheck_*`,
  `test_privacycheck_*`, `test_unknown_override_provider_prefix_fails_closed`,
  `test_unknown_slot_primary_prefix_*`, `test_unclassifiable_provider_is_blocked_on_the_strict_tier`)
  and the counter still 0, because the gate raises *before* `_call_with_fallback`; plus
  `test_unknown_override_prefix_is_counted_as_cloud_even_when_the_sink_is_local` (`== 1`, the
  `provider_prefix` param carries the override), `test_a_tier_approved_local_override_and_the_destination_disagree`
  (`== 0`, prefix `ollama` is local), `test_counter_equality_per_dispatch_site` (1 == 1 on all four
  sites, once §5.1 lands), `test_bundled_registry_holds_unclassifiable_cloud_providers`,
  `test_is_cloud_classifies_by_name_and_cannot_see_base_url`, both negative controls.
- `tests/chaos/test_chaos_gateway_faults.py`: `test_retries_are_exactly_three_attempts_for_a_transient_fault`,
  `test_in_process_matrix_records_an_outcome_per_fault_class`,
  `test_gateway_test_command_with_a_fault_is_a_clean_exit_1`, `test_healthy_local_stream_completes_with_chunks_and_done`
  (the healthy SSE stream's provider **does** send `finish_reason: "stop"`, §6),
  `test_negative_control_*`.
- `tests/chaos/test_chaos_accounting.py`: `test_successful_call_logs_exactly_one_row_and_counts_one`
  (one dispatch → 1 row, 1 count), `test_retry_delay_is_fixed_with_no_jitter_or_cap`,
  `test_retries_configured_as_zero_means_one_attempt`, and all `RecoveryCoordinator` tests.
- `tests/unit/test_gateway_router.py`: `test_returns_response_text`, `test_chat_survives_cost_logging_failure`,
  `test_falls_back_to_fallback_model` (the fallback `anthropic/claude-3` passes the performance-tier gate
  + PII flag and is dispatched), `test_raises_all_providers_failed`,
  `test_classify_error_*`, `test_auth_error_has_provider_attr`, `TestRerank::*` including
  `test_rerank_error_names_provider` and `test_rerank_applies_provider_credentials` (retries are
  invisible when the first attempt succeeds),
  `TestCustomProviderRouting::*` (`_get_litellm_kwargs` behaviour is preserved by the §3.2 extraction),
  `test_stream_idle_timeout_cuts_stalled_provider` (the httpx `ReadTimeout` branch still fires before
  the terminal check; `_record_cloud_call` is stubbed, so the signature requirement holds),
  `test_embedding_slot_chat_only_model_raises_pre_network`.
- `tests/integration/test_gateway_streaming.py` (both tests): `_record_cloud_call` monkeypatch still
  applies; `setdefault("timeout", ...)` preserves the dual `httpx.Timeout`.
- `tests/unit/test_gateway_errors.py`, `tests/unit/test_cloud_call_counter.py`,
  `tests/unit/test_cli_exception_formatting.py`, `tests/integration/tui/test_status_bar_egress.py`,
  `tests/integration/tui/test_explore_egress_modal.py`: untouched behaviour (module-counter API and
  `_format_exception` shape unchanged; redaction is additive and no test string matches the patterns).

### 4.5 New tests the Plan stage should add (TDD anchors, one per contract)

1. `_call_with_fallback` counts a failed attempt: 2 failures then success → 3 counts (unit, cloud registry).
2. Fallback retargeting: permissive tier, primary `openai/...`, fallback `anthropic/...` → the fallback
   dispatch carries `api_base == https://api.anthropic.com/v1` (the positive twin of the inverted
   `test_fallback_model_is_dispatched_on_the_strict_tier`).
3. Permanent short-circuit: 401 on a cloud primary → 1 dispatch, typed `AuthError` (unit, outside chaos).
4. `redact_text` masks a registered non-`sk-` secret and the literal `OPENAI_API_KEY` name.
5. `_iter_stream` raises `TruncatedStreamError` when the wrapper exposes no terminal attribute, and
   yields `done` when it exposes `received_finish_reason="stop"`.
6. `chat_stream` writes **no** `cost_logs` row when the stream is truncated, and exactly one on a
   terminal stream.

---

## 5. Test harness changes (to model honest behaviour)

### 5.1 `tests/redteam/_w5_probe.py` — the stream double
`DispatchRecorder.completion(stream_mode=True)` returns `iter([_Chunk("he"), _Chunk("llo")])`
(`:316`), a bare list iterator. Replace with a terminated-stream double:

```python
class StreamResponse:
    """What litellm returns for a stream that ended with a finish_reason."""

    def __init__(self, chunks: list[_Chunk], terminal: str = "stop") -> None:
        self.chunks = chunks
        self.received_finish_reason = terminal
        self.intermittent_finish_reason = terminal

    def __iter__(self) -> Iterator[_Chunk]:
        return iter(self.chunks)
```
and give `_Chunk` a `finish_reason` attribute (realism: the last chunk carries `"stop"`).
Needed by `tests/redteam/test_redteam_egress_guard.py::test_counter_equality_per_dispatch_site`
(`chunks[-1].type == "done"`).

### 5.2 `tests/unit/test_gateway_router.py` — the streaming fakes
`_make_chunk` (`:937`) and the `completion` lambdas in the three stream tests must return the same
shape as §5.1 (a small local double with `received_finish_reason="stop"`). Without it, three unit
tests would be asserting the *truncation* path by accident.

### 5.3 `tests/chaos/_w7_probe.py` — config, not shape
`streaming_gateway` (`:586-623`) builds `gw._config` with no `gateway.fallback` block, so
`_call_with_fallback` falls back to `retries=2, retry_delay=1.0` → the provider_down stream case would
sleep 2 s. Add `"fallback": {"retries": 2, "retry_delay": 0.0, "timeout": 5}` for determinism/speed.
The real SSE server, `CHUNK`/`CHUNK2`/`DONE`, `StreamOutcome` and `collect_stream` need **no** change —
they already carry a genuine `finish_reason` in the healthy mode.

### 5.4 Must NOT change (they are the anti-vacuity anchors)
- `_w5_probe.assert_no_dispatch` and both `test_negative_control_*` oracles.
- `_w7_probe.assert_clean_failure`, `_w7.StreamOutcome.completed`,
  `assert_stream_degraded_honestly`, `assert_typed_gateway_error`.
- `_w5_probe.patch_gateway_factory` / `gateway_forbidden` reachability counters.
- The `_record_cloud_call` monkeypatch points: its **name and signature** must survive verbatim
  (`_record_cloud_call(slot, provider_prefix=None)`).

---

## 6. The `finish_reason` puzzle — answer and evidence

**Question:** with the real local SSE server in mode `mid_stream_disconnect` (one chunk with
`"finish_reason": null`, then an abrupt close), `_iter_stream` yields `done` with no exception. What
does litellm actually yield for these truncated streams, and is any chunk's `finish_reason` truthy?

**Answer: yes — in every truncated case, at least one chunk carries a truthy `finish_reason`
(`"stop"`), because litellm fabricates it on EOF.** The fabricated value comes from
`CustomStreamWrapper.__next__`'s `except StopIteration:` branch, which calls `finish_reason_handler()`
(`litellm_core_utils/streaming_handler.py:1875-1888`):

```python
    _finish_reason = self.received_finish_reason or self.intermittent_finish_reason
    if _finish_reason is not None:
        model_response.choices[0].finish_reason = _finish_reason
    else:
        model_response.choices[0].finish_reason = "stop"      # <-- fabricated
```

So a chunk-level `finish_reason` predicate reports a truncated stream as **complete** — it is not a
terminal-detection predicate at all.

**Measured** (throwaway scripts under the session scratchpad, run with the worktree venv; exact
`tests/chaos/_w7_probe.py` payloads via its own `LocalSSEServer`):

| server mode | chunks `(content, chunk.finish_reason)` | `received_finish_reason` | `intermittent_finish_reason` | predicate (§3.6) |
|---|---|---|---|---|
| `healthy` (`CHUNK`,`CHUNK2`(`finish_reason:"stop"`),`[DONE]`) | `('hi',None) ('!',None) (None,'stop')` | `'stop'` | `'stop'` | **True** |
| `mid_stream_disconnect` (`CHUNK` then close) | `('hi',None) (None,'stop')` | `None` | `None` | **False** |
| `truncated_stream` (partial frame then close) | `(None,'stop')` | `None` | `None` | **False** |
| `malformed_body` | raises `litellm.MidStreamFallbackError` on the **first `next()`** | `None` | `None` | n/a |
| `data: [DONE]` with no finish_reason-carrying chunk | `('hi',None) (None,'stop')` | `None` | `None` | **False** |
| `finish_reason:"length"` then `[DONE]` | `('hi',None) (None,'length')` | `'length'` | `'length'` | **True** |
| empty body then close | `(None,'stop')` | `None` | `None` | **False** |

**Correct predicate:** `bool(getattr(response, "received_finish_reason", None) or
getattr(response, "intermittent_finish_reason", None))` — exactly the pair litellm itself consults
before fabricating a marker, so the gateway and litellm agree on what "the provider signalled" means.
The chunk-level `finish_reason` is never consulted.

**Eager vs lazy (also measured, decides §3.5):** `completion(stream=True)` raises **eagerly** for a
refused connection (`litellm.InternalServerError`, `Connection error.`) but returns a wrapper for a
malformed body, raising `litellm.MidStreamFallbackError` only on the first `next()`. Both must be
classified, hence both the dispatch-level and the iteration-level fix.

**Known false positive (fail-closed, accepted):** a provider that closes with `data: [DONE]` but never
sends a `finish_reason`-bearing chunk is reported as truncated. OpenAI-compatible providers send one;
the failure direction is "report incomplete", never "claim success", which is what #152 asks for.

**Drift guard:** if litellm renames these attributes, *every* stream is reported truncated and the
positive control `test_healthy_local_stream_completes_with_chunks_and_done` fails loudly on the next
litellm bump — a fail-closed, visible failure mode.

---

## 7. Open questions, risks, and what could not be verified

1. **`received_finish_reason` is a private litellm attribute.** Verified present in the pinned litellm
   (`streaming_handler.py:131-133`) and exercised by all W7b modes. Access is `getattr`-guarded, and
   §6's drift guard makes a rename loud. The alternative considered — `stream_options={"include_usage": True}`
   and requiring a usage chunk — changes the request payload and is not universally supported; rejected.
2. **A counter over-count of connection-refused attempts** (C1). Documented in the privacy footer's
   semantics ("dispatches attempted"), not a defect: the process cannot see whether bytes left.
3. **`_record_cloud_call` now runs before the dispatch** — a bookkeeping exception would abort the
   call. It only calls `load_registry()` (already called by the gate on the same path) and mutates two
   integers; no new wrap is proposed, but the Plan stage should decide whether to `try/except`-log it
   (fail-closed on the counter, fail-open on dispatch). No test covers it either way.
4. **`EmptyResponseError` changes recovery routing** for the no-choice shape: it previously crashed raw
   (a raw `AttributeError`/`IndexError`, i.e. not a gateway error at all), now it is a `GatewayError` in
   the recovery layer's `unknown` category → `user_guided_recovery`. If the product wants it treated as
   transient (`provider_fallback`), reuse `UnclassifiedProviderError` instead — the probe only needs
   `GatewayError` + the provider name. **Decision needed from the Plan stage.**
5. **Two error classes added** (`EmptyResponseError`, `TruncatedStreamError`). Cheaper alternative for
   #152: raise the existing `UnclassifiedProviderError` — rejected because it carries no `.provider`
   and its recovery meaning is "transient, try another provider", which is wrong once output has been
   streamed.
6. **`embed`/`rerank` reply-shape guards are not covered by any issue or probe** (`response.data`
   `:754`, `response.results` `:809`). Left as-is to keep the diff inside the filed defects; the same
   crash class is real (`tests/chaos` has no embed/rerank empty-reply fault).
7. **Zero-cost streaming rows remain** (`CostTracker.log_call` reads `response.usage`, absent on the
   dash wrapper) — the row is now written only for a genuinely terminated stream, which is what #150
   asks; capturing real stream usage needs `litellm.stream_chunk_builder(chunks=response.chunks)`
   (out of scope, noted for a follow-up issue).
8. **Fallback `retry_delay` defaults** (`1.0 s`) now apply to stream dispatches whose config lacks a
   `gateway.fallback` block (§5.3) — a test-runtime effect, not a product one.
9. **Not re-derived: a fallback that is *local* while the primary is cloud** now legitimately retargets
   `api_base` to the local host. That is the intended #144 behaviour but it is a *new* code path with no
   probe; §4.5 test 2 is the TDD anchor.
10. **#144's api_base bound is not re-proven for the primary path.** The issue's own bounding note (the
    4th dispatch carried the primary's `api_base`) is fixed on the fallback path only; the `model=`
    override path keeps dispatching with the primary's `api_base` (RT-032, deliberately out of scope).
