# Fix 10 Open Issues — Implementation Plan

> **For agentic workers:** Use tasks as checkboxes (`- [ ]`) for tracking.
>
> **Process note:** Produced with the **writing-plans** skill and ordered by the
> **test-driven-development** skill. Every repro test already exists as
> `xfail(strict=True)` on the pre-fix base, so a task's RED step runs the repro and confirms it
> xfails, and its GREEN step removes the strict marker and confirms PASS. Tasks 1 and 2 carry an
> explicit RED command; the remaining tasks rely on the shared repro baseline taken in Task 0
> (Step 3), since their repro node is already red for the same reason.
>
> **Base decision (blocks execution):** the working tree currently contains a prior, un-reviewed
> implementation of all ten fixes (Open Decision 1). Before Task 1 the base must be chosen:
> **(a)** carry that diff into the worktree — then skip each task's RED step and verify the fix is
> already present via `git diff`, or **(b)** `git restore` it and run every task under TDD as
> written. The baseline in Task 0 Step 3 expects the pre-fix count `49 passed, 24 xfailed` (option
> b); on a carried-over tree it is `72 passed, 2 xfailed`.
>
> Reviewed with **caveman-review** + **ponytail-audit** (full Review Mode) before execution.

**Goal:** Fix ten open `openreview` GitHub issues — each a boundary where hostile or corrupt
input escapes as a raw exception (or a silent default) instead of a typed, user-facing failure —
without changing any public contract beyond the defect.

**Architecture:** Three disjoint workstreams. **A) CLI/config boundary** (#139/#137/#138/#148): the
loader raises a typed `ConfigLoadError` for structural YAML problems; the CLI's `_init` and two
commands map `ConfigLoadError`, `ValidationError` and `AuthCorruptError` to `errors.config_error`
(exit 5). **B) playbook loader** (#136): `_parse_category` rejects a non-mapping with
`PlaybookLoadError`. **C) LLM-JSON / review parsers** (#132/#131/#134/#135/#133): `_parse_response`
and `_parse_json` require a JSON object, non-numeric/out-of-range confidence falls back, and
`extract_clause` records a parse failure on `assessment.error`.

**Tech Stack:** Python 3.12 pinned, `uv` only, pytest 9 (sockets disabled), Ruff + mypy strict,
Conventional Commits.

## Global Constraints

- Python 3.12; `uv run ...` for everything. No new dependencies.
- `uv run ruff check . && uv run ruff format .` and `uv run mypy src/ tests/` (strict) must pass.
- No explanatory comments unless the logic is non-obvious (repo style). Delete unused code.
- Conventional Commits (`fix:`/`test:`/`docs:`/`chore:`); never edit `[tool.ruff]`/`[tool.mypy]`/
  pytest config. Never `git add -A` — stage only the paths named in each task.
- Do not change: the pydantic config model, `config_set`'s `except (KeyError, ValidationError)`,
  `ClauseAssessment.__post_init__`, `strip_fences`, the TUI playbook-scan `except`, the 0600
  `auth.json` write mode, `load_auth`, the gateway wizard, or the healthy-run exit codes.
- `str(exc)` is the message form for `config_error(...)` (matches `config_set`).
- Tests must run offline (`--disable-socket`); use `-p no:cacheprovider` locally.
- **Anti-goal (register):** do not change RT-016's signal path (#134, Task 9) in the same commit as
  the RT-014 dict guard (#132, Task 6) — Tasks 6 and 9 are separate commits, and Task 9 must not
  alter `_parse_response`'s dict contract.
- **R-21 resolution (the register prose is stale):** R-21's acceptance says `load_config` catches
  `ValidationError`, but its own dependent test
  `tests/fuzz/test_fuzz_config_yaml.py::test_library_load_config_reaches_the_pydantic_validation`
  asserts `load_config` still raises it raw. The test is authoritative: the pydantic catch lives in
  the CLI `_init` (Task 3), `load_config` is unchanged, and R-21's wording should be corrected.
- **Audited-but-deferred (`#138` scope):** the TUI auth write path (`tui/domain/gateway.py`
  `_save_key`) and the `auth.json`-is-valid-JSON-but-not-an-object shape are the same *class* as
  #138/#148 but have no filed issue or repro node; they are deliberately out of scope here (Open
  Decision 2). State this in the PR description rather than silently widening the diff.

---

## Verified defect evidence

All measured on the current tree; each repro node is `xfail(strict=True)` today.

| # | Claim | Evidence (observed) |
|---|-------|---------------------|
| #138 | `save_key`/`save_provider_credentials` parse unguarded | corrupt `auth.json` → `json.JSONDecodeError` escapes; `gateway provider add --cred` exits 1, empty output |
| #139 | three `yaml.safe_load` reads unchecked | corrupt YAML → `yaml.ParserError`; top-level list → `AttributeError("'list' object has no attribute 'items'")`; CLI exits 1 |
| #137 | `load_config` never catches pydantic | `privacy.tier: 42` → raw `ValidationError`, CLI exits 1 |
| #148 | `gateway setup` calls the wizard head-less | corrupt `auth.json` → `AuthCorruptError` escapes, exit 1, `output == ""` |
| #136 | `_parse_category` does `dict(raw)` unchecked | `categories: [1, 2]` → raw `TypeError: 'int' object is not iterable`; TUI scan crashes |
| #132 | `_parse_response` assumes `.get` | reply `[1,2]` / `"hi"` / `null` → `AttributeError` |
| #131 | confidence reaches `__post_init__` unchecked | `"confidence": 5.0` → `ValueError("confidence must be in range 0.0-1.0")` escapes |
| #134 | parse failure is silent | malformed reply → default assessment, `error is None` |
| #135 | `fence_safe` whole-run blind spot | `"`````".replace("```","` ` `")` still contains `"```"` |
| #133 | `_parse_json` returns non-dicts | `null`/`"hi"`/`[1,2]` → `.get` raises in `qa._parse_qa_response` |

Baseline (whole repro set, pre-fix): `49 passed, 24 xfailed`.

## File structure

- **Modify** `src/openreview_cli/config/auth.py` — add `_read_auth_json`; use it in `load_auth`,
  `save_key`, `save_provider_credentials` (Task 1).
- **Modify** `src/openreview_cli/config/loader.py` — add `ConfigLoadError` + `_read_config_mapping`;
  use it at the three read sites (Task 2).
- **Modify** `src/openreview_cli/app.py` — `_init` catch (Task 3), `gateway_setup` catch (Task 4),
  `provider_add` catch (Task 1's CLI half).
- **Modify** `src/openreview_cli/review/playbook.py` — `_parse_category` guard (Task 5).
- **Modify** `src/openreview_cli/llm_json.py` — `fence_safe` (Task 8).
- **Modify** `src/openreview_cli/review/extraction.py` — `_parse_response_or_none`, `_parse_response`,
  `extract_clause` (Tasks 6, 7, 9).
- **Modify** `src/openreview_cli/review/prompts.py` — `_parse_json` (Task 10).
- **Modify tests** — remove the named `xfail(strict=True)` markers; invert the `#148` pin. No other
  assertion changes (Task 2 also updates the three library tests whose oracle changes).
- **Create** `docs/specs/plans/2026-09-27-fix-10-open-issues.md` (this plan).

---

### Task 0: Isolated workspace

**Files:** none (git only).

- [ ] **Step 1: Confirm the starting point**

```bash
git status --short
git branch --show-current
```

Expected: `main`, clean or with only the prior un-reviewed diff (Open Decision 1).

- [ ] **Step 2: Create the worktree/branch**

```bash
git worktree add .worktrees/fix-10-open-issues -b fix/10-open-issues
cd .worktrees/fix-10-open-issues
```

(If `.worktrees/` is not ignored, add it to `.git/info/exclude` first. If Open Decision 1 = (a),
carry the prior diff in: `git -C ../../ diff | git apply`.)

- [ ] **Step 3: Baseline the repro set**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_config_yaml.py tests/fuzz/test_fuzz_auth_json.py tests/fuzz/test_fuzz_playbook_yaml.py tests/fuzz/test_fuzz_llm_response.py tests/fuzz/test_fuzz_llm_fallbacks.py tests/fuzz/test_fuzz_prompt_escaping.py tests/redteam/test_redteam_gateway_setup.py
```

Expected: `49 passed, 24 xfailed`.

---

### Task 1: #138 — guard the auth write path

**Files:**
- Modify: `src/openreview_cli/config/auth.py`
- Modify: `src/openreview_cli/app.py` (`provider_add`, ~:1737)
- Test: `tests/fuzz/test_fuzz_auth_json.py`

**Interfaces:**
- Produces: `_read_auth_json(path: Path) -> dict[str, Any]` — raises `AuthCorruptError` on a JSON
  decode failure; `load_auth` keeps its env-override + missing-file behaviour.
- Consumes: `errors.config_error`.

- [ ] **Step 1: Confirm the repro fails (RED)**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_auth_json.py
```
Expected: 3 xfailed (`test_auth_corrupt_credentials_file_is_a_clean_config_error`,
`test_save_key_corrupt_file_raises_auth_corrupt_error`,
`test_save_provider_credentials_corrupt_file_raises_auth_corrupt_error`).

- [ ] **Step 2: Implement**

`config/auth.py`:
```python
def _read_auth_json(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AuthCorruptError(
            f"{path} is corrupt: {exc}. Fix or delete the file and rerun `openreview gateway setup`."
        ) from exc
    return data
```
`load_auth` uses it for its existing read; `save_key` and `save_provider_credentials` call it
instead of `json.loads(...)`. In `app.py:provider_add`, import `AuthCorruptError` alongside
`save_provider_credentials` and wrap the call:
```python
        try:
            save_provider_credentials(get_config_dir() / "auth.json", name, parsed)
        except AuthCorruptError as exc:
            config_error(str(exc))
```

- [ ] **Step 3: Remove the 3 xfail decorators; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_auth_json.py
```
Expected: `5 passed`.

- [ ] **Step 4: Regression + lint + commit**

```bash
uv run pytest -q -p no:cacheprovider tests/unit/test_auth.py
uv run ruff check src/openreview_cli/config/auth.py src/openreview_cli/app.py
uv run mypy src/openreview_cli/config/auth.py src/openreview_cli/app.py
git add src/openreview_cli/config/auth.py src/openreview_cli/app.py tests/fuzz/test_fuzz_auth_json.py
git commit -m "fix(auth): guard the auth.json write path against a corrupt file"
```

---

### Task 2: #139 — typed config error at all three loader sites

**Files:**
- Modify: `src/openreview_cli/config/loader.py`
- Test: `tests/fuzz/test_fuzz_config_yaml.py`

**Interfaces:**
- Produces: `ConfigLoadError(ValueError)`; `load_config`/`set_config_value`/`add_custom_provider`
  raise it for a `yaml.YAMLError` or a non-mapping top level.
- Consumes: nothing new.

- [ ] **Step 1: Confirm the repro fails (RED)**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_config_yaml.py
```
Expected: 3 xfailed CLI params + the 3 library tests pass while asserting raw `yaml.YAMLError`.

- [ ] **Step 2: Implement**

```python
class ConfigLoadError(ValueError):
    """config.yml could not be read as a mapping."""


def _read_config_mapping(config_path: Path) -> dict[str, Any]:
    import yaml

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"{config_path} must contain a mapping at the top level")
    return raw
```
Use `_read_config_mapping(config_path)` in `load_config`, `set_config_value` and
`add_custom_provider` (replacing their `yaml.safe_load(...) or {}` reads). Do not touch
`_validate_and_merge` or the model.

- [ ] **Step 3: Update the three library tests to the new oracle**

In `tests/fuzz/test_fuzz_config_yaml.py`, change `test_library_load_config_reaches_the_yaml_parse`,
`test_library_set_config_value_reaches_the_yaml_parse` and
`test_library_add_custom_provider_reaches_the_yaml_parse` from `pytest.raises(yaml.YAMLError)` to
`pytest.raises(loader.ConfigLoadError)`; rename each to `..._maps_a_yaml_error_to_config_load_error`;
keep every `count_calls` assertion. Remove the now-unused `import yaml` if nothing else uses it.

- [ ] **Step 4: Remove the `corrupt-yaml` + `list-not-map` xfails; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_config_yaml.py
```
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/openreview_cli/config/loader.py tests/fuzz/test_fuzz_config_yaml.py
git commit -m "fix(config): map a malformed or non-mapping config.yml to ConfigLoadError"
```

---

### Task 3: #137 — map pydantic validation of config to exit 5

**Files:**
- Modify: `src/openreview_cli/app.py` (`_init`, ~:264)
- Test: `tests/fuzz/test_fuzz_config_yaml.py`

- [ ] **Step 1: Implement** — in `_init`, around the `load_config` call:
```python
    from pydantic import ValidationError

    try:
        config = load_config(config_dir / "config.yml")
    except (ConfigLoadError, ValidationError) as exc:
        config_error(str(exc))
```
Import `ConfigLoadError` from `openreview_cli.config.loader`. Do **not** change `load_config`
(the dependent test asserts it still raises raw `ValidationError`).

- [ ] **Step 2: Remove the `wrong-typed` xfail; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_config_yaml.py::test_config_malformed_cli_is_a_clean_config_error tests/fuzz/test_fuzz_config_yaml.py::test_library_load_config_reaches_the_pydantic_validation
```
Expected: 4 passed (3 params + the library test).

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/app.py tests/fuzz/test_fuzz_config_yaml.py
git commit -m "fix(config): surface a wrongly typed config.yml as exit 5"
```

---

### Task 4: #148 — `gateway setup` corrupt auth.json

**Files:**
- Modify: `src/openreview_cli/app.py` (`gateway_setup`, ~:1411)
- Test: `tests/redteam/test_redteam_gateway_setup.py`

- [ ] **Step 1: Implement**
```python
    from openreview_cli.config.auth import AuthCorruptError

    try:
        _wizard()
    except AuthCorruptError as exc:
        config_error(str(exc))
```

- [ ] **Step 2: Tests** — remove `@pytest.mark.xfail(strict=True, reason="RT-033")` from
  `test_gateway_setup_on_a_corrupt_auth_json_is_a_clean_config_error`. **Invert** the pinning node
  `..._escapes_with_no_message` (register R-23) into
  `test_gateway_setup_on_a_corrupt_auth_json_names_the_file`, asserting
  `assert_clean_failure(result, frozenset({EXIT_CONFIG}), "corrupt")` **and** that the message names
  the corrupt file (`"auth.json" in result.output`) — so it is not a byte-for-byte duplicate of the
  expectation test but a distinct assertion about the message.

- [ ] **Step 3: Verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/redteam/test_redteam_gateway_setup.py
```
Expected: 7 passed, 1 xfailed (RT-034, out of scope).

- [ ] **Step 4: Commit**

```bash
git add src/openreview_cli/app.py tests/redteam/test_redteam_gateway_setup.py
git commit -m "fix(gateway): report a corrupt auth.json from setup as exit 5"
```

---

### Task 5: #136 — reject scalar playbook categories

**Files:**
- Modify: `src/openreview_cli/review/playbook.py` (`_parse_category`, ~:123)
- Test: `tests/fuzz/test_fuzz_playbook_yaml.py`

- [ ] **Step 1: Implement** — first statement of `_parse_category`:
```python
    if not isinstance(raw, dict):
        raise PlaybookLoadError("each category must be a mapping")
```
Guard goes **inside** `_parse_category` (the repro counts its calls). Do not widen the TUI scan.

- [ ] **Step 2: Remove both `RT-024` xfails; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_playbook_yaml.py
uv run pytest -q -p no:cacheprovider tests/unit/test_playbook.py tests/unit/test_playbook_diff.py tests/unit/test_playbook_schema.py
```
Expected: fuzz `8 passed`; unit all pass.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/review/playbook.py tests/fuzz/test_fuzz_playbook_yaml.py
git commit -m "fix(playbook): reject a non-mapping category with PlaybookLoadError"
```

---

### Task 6: #132 — `_parse_response` requires a JSON object

**Files:**
- Modify: `src/openreview_cli/review/extraction.py` (`_parse_response`, ~:161)
- Test: `tests/fuzz/test_fuzz_llm_response.py`

- [ ] **Step 1: Implement** — after `json.loads`, return the fallback when the value is not a
  `dict`; keep the fallback dict exactly
  `{"position": "uncertain", "confidence": 0.0, "citation": "", "category_match": False}`.
  Do not touch `extract_clause` in this task (anti-goal).

- [ ] **Step 2: Remove the `RT-014` xfail; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_llm_response.py
```
Expected: `test_non_object_reply_still_returns_a_dict` passes; RT-017 still xfailed.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/review/extraction.py tests/fuzz/test_fuzz_llm_response.py
git commit -m "fix(review): return the fallback for a non-object extraction reply"
```

---

### Task 7: #131 — clamp/drop an out-of-range or non-numeric confidence

**Files:**
- Modify: `src/openreview_cli/review/extraction.py` (`_parse_response`)
- Test: `tests/fuzz/test_fuzz_prompt_escaping.py`

- [ ] **Step 1: Implement** — coerce `confidence`; on `TypeError`, `ValueError`, or `OverflowError`
  (a huge integer literal, e.g. `10**400`, makes `float()` raise `OverflowError`) or a value outside
  `[0.0, 1.0]`, return the whole fallback dict (uncertain, 0.0). Do not change `ClauseAssessment`.
  If the base is carried over, widen the delivered `except (TypeError, ValueError)` to include
  `OverflowError`.

- [ ] **Step 2: Remove the `RT-019` xfail; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_prompt_escaping.py
```
Expected: `test_out_of_range_confidence_does_not_escape` passes; RT-018 xfailed (Task 8).
Optionally parametrise that test with a huge-int confidence (`{"confidence": 10**400}`) so the
`OverflowError` branch is covered; assert `position is Position.UNCERTAIN and confidence == 0.0`.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/review/extraction.py tests/fuzz/test_fuzz_prompt_escaping.py
git commit -m "fix(review): drop an out-of-range confidence instead of raising"
```

---

### Task 8: #135 — neutralise every backtick run

**Files:**
- Modify: `src/openreview_cli/llm_json.py`
- Test: `tests/fuzz/test_fuzz_prompt_escaping.py`

- [ ] **Step 1: Implement**
```python
import re

def fence_safe(text: str) -> str:
    return re.sub(r"`{3,}", lambda m: " ".join(m.group()), text)
```
Do not change `strip_fences`.

- [ ] **Step 2: Remove the `RT-018` xfail; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_prompt_escaping.py
uv run pytest -q -p no:cacheprovider tests/unit/test_llm_json.py
```
Expected: fuzz `test_fence_safe_neutralises_every_backtick_run[five/eight]` pass.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/llm_json.py tests/fuzz/test_fuzz_prompt_escaping.py
git commit -m "fix(llm-json): neutralise backtick runs of any length in fence_safe"
```

---

### Task 9: #134 — signal a parse failure from `extract_clause`

**Files:**
- Modify: `src/openreview_cli/review/extraction.py` (`extract_clause`, `_parse_response_or_none`)
- Test: `tests/fuzz/test_fuzz_llm_response.py`

**Interfaces:**
- Produces: `_parse_response_or_none(raw: str) -> dict[str, Any] | None`; `_parse_response` delegates
  (public contract unchanged); `extract_clause` sets `assessment.error` on a `None`.

- [ ] **Step 1: Implement** — extract the guards added in Tasks 6–7 into a helper that reports
  failure with `None`, keep `_parse_response` as the documented public contract (it delegates, so
  the RT-014/RT-019 dict contract is unchanged), and have `extract_clause` use the helper:
```python
_SAFE_DEFAULT_RESPONSE: dict[str, Any] = {
    "position": "uncertain", "confidence": 0.0, "citation": "", "category_match": False,
}


def _parse_response_or_none(raw: str) -> dict[str, Any] | None:
    """Return the four-key response dict, or ``None`` for any unusable reply."""
    try:
        data = json.loads(strip_fences(raw))
    except ValueError:  # json.JSONDecodeError subclasses ValueError
        return None
    if not isinstance(data, dict):
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return {
        "position": str(data.get("position", "uncertain")),
        "confidence": confidence,
        "citation": str(data.get("citation", "")),
        "category_match": bool(data.get("category_match", False)),
    }


def _parse_response(raw: str) -> dict[str, Any]:
    """Public parser contract (RT-014): always returns the four-key dict."""
    parsed = _parse_response_or_none(raw)
    return dict(_SAFE_DEFAULT_RESPONSE) if parsed is None else parsed
```
and in `extract_clause`, inside the existing `try`:
```python
        parsed = _parse_response_or_none(raw_response)
        if parsed is None:
            raise ValueError(  # noqa: TRY301 - routed through the handler below
                "extraction response was not a usable JSON object"
            )
```
(the `raise` keeps one fallback path: the existing `except Exception` sets `error=str(exc)`; the
`noqa` is justified — `ruff` TRY301 fires on the shape and the comment records the security-relevant
fact that a static message, never the reply text, is recorded). Do not log the raw reply.

- [ ] **Step 2: Remove the `RT-016` xfail; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_llm_response.py
uv run pytest -q -p no:cacheprovider tests/unit/test_extraction_agent.py
```
Expected: `test_malformed_reply_is_signalled_not_silent` passes; the safe-default and happy-path
tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/review/extraction.py tests/fuzz/test_fuzz_llm_response.py
git commit -m "fix(review): signal a malformed extraction reply on assessment.error"
```

---

### Task 10: #133 — `_parse_json` returns the fallback for a non-dict

**Files:**
- Modify: `src/openreview_cli/review/prompts.py` (`_parse_json`, ~:11)
- Test: `tests/fuzz/test_fuzz_llm_fallbacks.py`

- [ ] **Step 1: Implement**
```python
def _parse_json(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(strip_fences(raw))
    except ValueError:  # json.JSONDecodeError subclasses ValueError
        return fallback
    if not isinstance(data, dict):
        return fallback
    return data
```

- [ ] **Step 2: Remove both `RT-015` xfails; verify GREEN**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz/test_fuzz_llm_fallbacks.py
```
Expected: `test_prompts_parse_json_never_returns_a_non_dict` and
`test_qa_parse_response_never_raises_on_a_non_object` pass; `qa._parse_qa_response` inherits it.

- [ ] **Step 3: Commit**

```bash
git add src/openreview_cli/review/prompts.py tests/fuzz/test_fuzz_llm_fallbacks.py
git commit -m "fix(review): return the caller fallback for a non-object QA/extraction reply"
```

---

## Final verification (Lead QC)

- [ ] **Repro + neighbouring suites**

```bash
uv run pytest -q -p no:cacheprovider tests/fuzz tests/redteam
uv run pytest -q -p no:cacheprovider tests/unit/test_benchmark_receipts.py
uv run pytest -q -p no:cacheprovider tests/unit/test_auth.py tests/unit/test_config_loader.py tests/unit/test_cli_config.py tests/unit/test_extraction_agent.py tests/unit/test_llm_json.py tests/unit/test_playbook.py
uv run pytest -q -p no:cacheprovider tests/integration/test_config_change.py
```

- [ ] **Static checks**

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ tests/
```

- [ ] **Whole-branch review** (`caveman-review` + `ponytail-audit`, full Review Mode) before
  finishing the branch.

## Self-review against the requirements

- Every issue in the scope table maps to exactly one task, and every task names its repro node and
  its exact verification command.
- No task touches a forbidden file (`config_set` handler, model, `__post_init__`, `strip_fences`,
  TUI scan `except`, 0600 mode).
- Tasks 6 and 9 are separate commits (register anti-goal: RT-014 vs RT-016).
- The base choice (Open Decision 1) and the R-21 arbitration are recorded in the header, and the
  deferred same-class surfaces (#138/#148) are scoped out explicitly rather than silently.
- Types line up: `ConfigLoadError(ValueError)`, `_parse_response_or_none -> dict | None`,
  `_read_auth_json -> dict[str, Any]`, `_read_config_mapping -> dict[str, Any]`.
