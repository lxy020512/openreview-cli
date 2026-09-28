# Fix 10 open issues — design (logging, startup storage/config, TUI egress, retrieval, PII, graph)

**Scope:** #161, #160, #159, #158, #157, #145, #118, #116, #115, #108 — six seams:
`app.py::_init` (logging + startup guards), `config/auth.py` + `config_set`, `tui/domain/egress.py`
(+ `gateway/models.py`/`gateway/registry.py` for the litellm-free classifier), `retrieval/storage.py`
(+ the four callers), `pii/engine.py` (+ `pii/placeholders.py`, `pii/models.py`), and
`parsing/clause_detector.py` + `parsing/docx_parser.py` + `chunking/splitter.py`.

**Base:** worktree, detached at `30ffdc6` (`fix(gateway): make dispatch, accounting and egress
reporting truthful`). All ten fixes land on one branch + PR (approved decision 2).

**Measured there is no shared root cause.** These are ten independent defects; the only thing they
have in common is that each is an *unhandled* boundary. There is no "one seam" narrative here, and
this doc does not invent one.

**Deliverable of this doc:** the per-issue function-level design, the contract decisions that drive
the code, the exhaustive list of existing tests that pin the old behaviour, and the risks. Every
code reference below was re-read against the current tree (`file:line` is today's line, not the
issue's — the issue bodies' line numbers are stale, see §6.1). Claims marked *(measured)* were
verified with throwaway probes under the repo venv; probe sources are in the session scratchpad.

---

## 1. Scope and baseline

### 1.1 Which tests fail today

```
uv run pytest --runxfail -q -p no:randomly --no-header -p no:cacheprovider \
  tests/chaos/test_chaos_resource_limits.py::test_config_set_on_a_read_only_config_dir_is_a_clean_config_error \
  tests/chaos/test_chaos_resource_limits.py::test_a_read_only_data_dir_is_a_clean_error \
  tests/chaos/test_chaos_concurrency.py::test_a_cli_start_racing_an_open_migration_transaction_is_a_clean_error \
  tests/redteam/test_redteam_egress_guard.py::test_egress_summary_does_not_claim_cloud_for_a_local_provider \
  tests/redteam/test_redteam_egress_guard.py::test_egress_summary_warns_when_a_cloud_provider_is_named_local \
  tests/chaos/test_chaos_resource_limits.py::test_config_set_on_a_read_only_config_dir_escapes_today \
  tests/chaos/test_chaos_resource_limits.py::test_a_read_only_existing_database_escapes_with_no_message \
  tests/chaos/test_chaos_resource_limits.py::test_a_fresh_unwritable_data_dir_escapes_with_no_message \
  tests/chaos/test_chaos_concurrency.py::test_a_cli_start_racing_an_open_migration_transaction_crashes_locked \
  tests/redteam/test_redteam_egress_guard.py::test_is_cloud_classifies_by_name_and_cannot_see_base_url \
  tests/exploratory/test_explore_egress_counter.py::test_is_cloud_matches_only_literal_ollama_and_local \
  tests/exploratory/test_explore_egress_counter.py::test_local_vs_cloud_warning_requires_pii_disabled
=> 5 failed, 10 passed
```

The **5 failures** are the `xfail(strict=True)` expectation nodes (they go red under `--runxfail`):
3 chaos nodes for #157/#158/#159 and 2 red-team nodes for #145. The **10 passes** are the
characterisation pins (§4.2) plus the four `test_local_vs_cloud_warning_requires_pii_disabled`
parametrisations. Note the read-only cases do **not** skip here (euid 1000).

Fault reproduction, direct, in-process *(measured)*:

| scenario | exit | exception that escapes `_init` | output |
|---|---|---|---|
| read-only config dir, no `auth.json` | 1 | `PermissionError: [Errno 13] ... auth.json` | `''` |
| read-only data dir, migrated db | 1 | `sqlite3.OperationalError: attempt to write a readonly database` | `''` |
| fresh unwritable data dir | 1 | `sqlite3.OperationalError: unable to open database file` | `''` |
| cold start racing an open migration txn | 1 | `sqlite3.OperationalError: database is locked` | `''`, `user_version == 0` |

### 1.2 Two baseline corrections the plan must not skip

1. **Only 4 of the 10 issues have an `xfail` repro node.** There are 14 `xfail` sites in the whole
   test tree; 5 belong to this scope (#157/#158/#159/#145). **#161, #160, #118, #116, #115, #108
   have no repro node at all** — their baseline evidence is code reading, direct probes, and the
   characterisation pins in §4.2/§4.3. The TDD "RED step" for those six is a *new* test written in
   the first commit of the task, not an un-marking.
2. **Three of the six are pinned by tests that currently PASS and will break** (#118 fuzz pin,
   #115 FR-006 gate, #108 caveat docstring) — see §4.2 and §4.3. The brief's pin list is incomplete;
   §4 is the authoritative list.

### 1.3 Global constraints (repo invariants this design must not break)

- Forbidden: `langchain`, `llama-index`, `FAISS`, direct `spaCy` for PII, external logrotate-style
  tooling, `loguru`/`structlog`. **stdlib `logging` only** — so #161's rotation must be
  `logging.handlers.RotatingFileHandler`.
- RAM budget < 100 MB soft / 110 MB hard. Nothing here adds a buffer: the PII fix *removes*
  entities; the log fix adds one 10 MiB-on-disk handler; the graph fix is O(open depth).
- PII is stripped before egress, and **raw contract text must never be logged** (#160).
- `tui/` must not import litellm at module level (measured cost 3.17 s, and
  `tests/unit/tui/test_egress_summary.py::test_egress_module_import_does_not_pull_litellm` asserts
  it with a subprocess).
- `install_on_root_handlers()` must stay the **last** root-logging call in `_init` — a handler added
  after it never receives the `RedactingFilter` (`gateway/redaction.py:53-61`), which
  `tests/redteam/test_redaction_ordering.py:88` demonstrates as a live hole and
  `tests/unit/test_api_key_redaction_e2e.py:121` would catch in the log file. This constrains the
  #161 ordering (§3.1).
- XDG is **not** globally isolated (`isolated_xdg` is opt-in), so any new `load_registry()` call
  reachable from a test must be patched deterministically (§3.5, §5.5).

---

## 2. Contract decisions (settled here; they drive §3)

- **C1 — #161 keeps one log file family, two knobs.** A hard size cap (`RotatingFileHandler`) bounds
  disk; the configured retention is a *ceiling applied by an expiry pass at startup*. Python 3.12's
  `TimedRotatingFileHandler` does **not** accept `maxBytes` *(measured: its `__init__` has no
  `maxBytes` and `shouldRollover` is time-only)*, so a single stdlib handler cannot do both. See §3.1.
- **C2 — the retention window is `min(privacy.log_ttl_days, storage.logs_keep_days)`.** Two keys
  declare one window; the privacy-motivated one must not be weakened by the storage one, and `min`
  can only shorten retention. Both default to 30 (`config/loader.py:15,62,178,211`) and both are
  always present in `load_config`'s validated output, so no defaulting code is needed beyond `min`.
- **C3 — #160 logs the claim index + verdict + clause label, never clause text.** Not a hash, not a
  truncated prefix, not "redacted text": the 40-char prefix is raw contract text and hashing it adds
  a value nobody can use. The clause *citation* (`result.citation`, e.g. `4.3`) stays.
- **C4 — #157/#159 both get a bounded retry, and both still end in a named failure.** Retry with
  backoff around `PRAGMA journal_mode=WAL` + migrations (the race genuinely resolves); on exhaustion
  route to the existing `_init` storage branch. `busy_timeout` is explicitly **not** the fix (SQLite
  does not invoke the busy handler for a journal-mode change; the chaos docstring at
  `tests/chaos/test_chaos_concurrency.py:326-336` already measured this).
- **C5 — one storage failure, one exit code: `EXIT_USER_ERROR` (1)**, via the *existing*
  `fail(f"cannot open database {db_path}: {exc}", EXIT_USER_ERROR)` (`app.py:281-282`). No new error
  class, no new code; the message already names the component (`database`), which is what both chaos
  oracles assert.
- **C6 — one config failure, one exit code: `EXIT_CONFIG` (5)** via `config_error()` (`errors.py:46`),
  prefix `Config error`. Both #158 seams (the `_init` auth write and `config_set`) route there.
- **C7 — #145 classifies from the registry, and unresolvable is cloud.** `classify_provider` moves to
  `gateway/models.py` (litellm-free; it takes a `ProviderInfo`) and is re-exported from
  `gateway/router.py` so `from openreview_cli.gateway.router import classify_provider`
  (`tests/unit/test_gateway_router.py:22`, `tests/redteam/test_redteam_egress_guard.py:37`) keeps
  working. `ValueError` from `classify_provider` ("no base_url and not local") resolves **cloud** —
  the same verdict `_enforce_tier` reaches at `router.py:285/327`.
- **C8 — #145 keeps `_is_cloud(provider)` callable with one argument.** Its registry is an optional
  second parameter; `build_egress_summary` loads the registry **once** and passes it in (one JSON
  read per summary instead of one per destination), and the existing single-arg call sites in the
  red-team/exploratory pins keep working.
- **C9 — #118: the storage layer raises, the callers decide.** `get_index_meta` distinguishes
  *not-a-database* (missing table / unopenable file → `None`, unchanged) from *exists but unreadable*
  (any other `sqlite3.DatabaseError`, e.g. `database disk image is malformed` → `IndexCorruptError`).
  Per caller: `index-status` **reports damage** and exits **3**; `retrieve` keeps its existing
  corrupt mapping (exit 3, `app.py:2358-2360`); `ingest` keeps its broad `except Exception` → rebuild.
  Deliberately **not** the registry's `EXIT_RETRIEVAL_INDEX_CORRUPT = 41`: those codes are
  unreachable from `app.py` by design today and that is RT-003, a separate filed issue
  (`tests/exploratory/test_explore_retrieval_group.py::test_retrieval_registry_codes_are_unreachable_from_the_cli`).
- **C10 — #116 pins tldextract to the bundled snapshot in the engine (option 1), before the analyzer
  serves any request.** The product-side fix must be verifiable *independent of* the session fixture,
  so its test resets the module global to the network default first (§5.8). With option 1 taken the
  four public-claim reconciliations in the issue (README.md:11, README.md:127, PRODUCT.md:18,
  PRODUCT.md:65) **become unnecessary — no README/PRODUCT edit is in scope for #116**.
- **C11 — #115 resolves overlapping spans to one entity *before* placeholder assignment, and the
  FR-006 predicate is left alone.** The predicate was adopted deliberately (R8 amendment); reverting
  it re-opens CR/PR baselines. Consequence, *measured*, is that the span-level precision gate falls
  below 0.95 (§3.9) — that must be handled as an explicit re-baseline, not hidden.
- **C12 — #108 populates `parent_id` only; `Clause.level`, `DEFAULT_WEIGHTS` and
  `MAX_EXPECTED_DEPTH` are untouched.** The approved scope. Two of the six `parent_id=None` sites the
  issue lists are **synthetic singletons** (`grounding/discriminator.py:93`,
  `pii/engine.py:554`) where `None` is correct and stays (§3.10).

  > **Superseded (2026-09-28):** the health score was subsequently re-scoped to structural defects
  > only — `DEFAULT_WEIGHTS` is now `[0.0, 0.0, 0.35, 0.40, 0.25]` (density and depth score nothing),
  > so a defect-free document, flat or structured, scores 100 by design. See the task doc's
  > `## Superseded (2026-09-28)` note.

---

## 3. Design per issue (function level, order of operations)

### 3.1 #161 — bounded, expiring log file

Today (`app.py:237-290`), the order is: level → drop stale owned handlers → `FileHandler(log_file)`
(`:253`) → `StreamHandler` → `install_on_root_handlers()` (`:261`) → `load_config` (`:267`) →
`ensure_auth` (`:272`) → `init_database` (`:278`). The retention keys are read *nowhere*
(`grep log_ttl_days|logs_keep_days src/` → only `config/loader.py`).

New module-level constants (in `app.py`, next to `_init`):

```python
# Bound per segment, and the number of rotated segments kept beside it: the log family
# is capped at _LOG_MAX_BYTES * (_LOG_BACKUP_COUNT + 1) = 60 MiB, whatever the retention
# window says. Retention (§C2) is a ceiling applied on top of that, never a raise.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
```

New helper (pure, unit-testable without the CLI):

```python
def _log_retention_days(config: dict[str, Any]) -> int:
    """The effective log retention: the shorter of the two declared windows (C2)."""


def _expire_log_files(log_dir: Path, retention_days: int, *, now: float | None = None) -> int:
    """Delete log segments that cannot contain an entry newer than the window.

    A segment's mtime is its last write, so `mtime < cutoff` implies every entry in it
    is older than the window. A file whose content is younger than the cutoff is left
    alone, so this never deletes live data. Returns the number of files removed.
    """
```

`_init`'s new order (the **only** structural change):

```python
    _level = _log_level(debug=debug, verbose=verbose)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(_level)
    for _h in root.handlers[:]:                      # unchanged: drop stale owned handlers
        if getattr(_h, "_openreview_owned", False):
            root.removeHandler(_h); _h.close()

    _sh = logging.StreamHandler(sys.stderr)          # stderr first: usable before/without a log file
    _sh.setFormatter(_fmt)
    _sh._openreview_owned = True
    root.addHandler(_sh)

    config_dir = get_config_dir()
    try:
        config = load_config(config_dir / "config.yml")
    except (ConfigLoadError, ValidationError) as exc:
        config_error(str(exc))                       # stderr-only: the file handler does not exist yet
    logger.info("config loaded")

    with contextlib.suppress(Exception):             # housekeeping never fails a command
        _expire_log_files(log_dir, _log_retention_days(config))

    _fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8"
    )
    _fh._openreview_owned = True
    root.addHandler(_fh)

    install_on_root_handlers()                       # LAST (see §1.3) — unchanged relationship
    ...
```

Why this order:
- The file handler is still created **before** `install_on_root_handlers()`, so the redaction filter
  covers it. Creating it after would reopen the hole `tests/redteam/test_redaction_ordering.py:88`
  demonstrates.
- The expiry pass runs *before* the handler opens the file, so unlinking a stale active log cannot
  leave a live fd writing to an unlinked inode (the failure mode a post-open unlink would have).
- Cost of the reorder: a *fatal* config load error is reported on stderr only, not in the log file.
  It is printed by `config_error` either way, and at the default WARNING level nothing else logs in
  that window. Accepted; recorded in §6.
- I/O cost per command: one `os.stat` per existing segment (≤ 6 stats) and one `getmtime` compare.
  No scanning of file *contents* — the log reached 43,911,517 lines; reading it to expire it would
  be the bug, not the fix.

Exact expiry semantics: `cutoff = time.time() - retention_days * 86400`; for each of
`log_dir.glob("openreview.log*")` (the active file and `RotatingFileHandler`'s `openreview.log.1..N`
backups), unlink when `st_mtime < cutoff`. **No error code**: `_init` never fails because of logging
housekeeping (`contextlib.suppress` + `logger.debug`).

### 3.2 #160 — stop logging clause text

`grounding/models.py:105-112` (`CGReport.merge_into`):

```python
            if self.mode == "strict" and result.verdict in (
                GroundingVerdict.UNGROUNDED, GroundingVerdict.UNCERTAIN,
            ):
                logger.warning(
                    "Claim #%d excluded: %s in clause %s",
                    idx,
                    result.reason or result.verdict.value,
                    assessment.citation,
                )
```

Removed argument: `assessment.clause_text[:40]` (`:109`). The line keeps the claim index, the
verdict/reason, and the clause citation — everything needed to debug the exclusion, and nothing
that is contract text. No exit code, no new API; `result.reason` is model-produced and is **not**
redacted here (residual risk, §6.5). A hash was rejected (C3): unreadable and unactionable.

### 3.3 #157 / #159 — the storage step in `_init`

Current (`app.py:277-282`):

```python
    try:
        init_database(db_path)
    except sqlite3.OperationalError:
        raise                                    # <-- re-raises the two defects raw
    except sqlite3.DatabaseError as exc:
        fail(f"cannot open database {db_path}: {exc}", EXIT_USER_ERROR)
```

`sqlite3.OperationalError` is a **subclass** of `DatabaseError`, so this branch order is what makes
#157 ("attempt to write a readonly database", "unable to open database file") and #159 ("database is
locked") escape unhandled while a *malformed* database is already reported cleanly (the branch the
fuzz suite pins).

Two changes:

1. **`storage/database.py` gains a bounded retry around the journal-mode change** (the only place
   SQLite bypasses the busy handler):

```python
_WAL_RETRY_ATTEMPTS = 5
_WAL_RETRY_DELAY_S = 0.2          # 0.2 + 0.4 + 0.6 + 0.8 = 2.0 s worst case

def _enable_wal(conn: sqlite3.Connection) -> None:
    """Set WAL, retrying only the 'database is locked' case.

    SQLite does not invoke the busy handler for a journal-mode change while another
    connection holds a transaction (measured: fails in 0.00 s even with a 2000 ms
    busy_timeout), so this is a retry, not a timeout (C4).
    """
    for attempt in range(_WAL_RETRY_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _WAL_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_WAL_RETRY_DELAY_S * (attempt + 1))
```

   `get_connection` (`:13`) calls `_enable_wal(conn)` where it runs the pragma today (`:15`). A
   *read-only* directory still raises on the first attempt ("unable to open"/"readonly" are not
   "locked"), so #157 pays no retry latency.
2. **`_init` deletes its `except sqlite3.OperationalError: raise` branch** (`:279-280`), so every
   `sqlite3.DatabaseError` — including an exhausted lock retry — takes the existing
   `fail(..., EXIT_USER_ERROR)` path (**exit 1**, prefix `Error`, message containing
   `cannot open database <path>/openreview.db: ...`, which is what
   `assert_clean_database_failure`'s `"database" in output.lower()` needs).

`run_migrations` also runs `conn.execute("BEGIN")`+`_exec_migration_safely` (`:39-53`); a lock there
raises the same OperationalError and is covered by the same `_init` branch. The retry is deliberately
*not* wrapped around the whole migration: each migration is already an atomic transaction
(`_exec_migration_safely`, verified by `tests/fuzz/test_fuzz_sqlite.py::test_init_database_makes_a_usable_schema`),
so retrying the pragma is sufficient and re-running migrations is not needed.

### 3.4 #158 — the config step in `_init`, and `config_set`

Seam 1 (`app.py:272`): `ensure_auth(config_dir)` → `config/auth.py:26-32` →
`write_auth(auth_dir / "auth.json", {})` → `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)`
(`config/auth.py:72`) → `PermissionError` on a read-only directory.

```python
    try:
        ensure_auth(config_dir)
    except OSError as exc:
        config_error(f"cannot write {config_dir / 'auth.json'}: {exc}")
    logger.info("auth configured")
```

`OSError` (not only `PermissionError`) because a full disk / a read-only mount / a bad path all
reach the same call; all of them are the same defect class and all deserve exit 5 (**C6**).
`ensure_auth`'s `path.exists()` early return is untouched, so the normal path costs nothing.

Seam 2 (`app.py:511-515`): `config_set` catches only `(KeyError, ValidationError)`, while
`set_config_value` (`config/loader.py:319-340`) does `shutil.copy2(config_path, backup)` (`:327`)
and `open(config_path, "w")` (`:337`) — both raise `OSError` on a read-only config dir.

```python
    try:
        set_config_value(config_path, key, value)
        typer.echo(f"updated {key} = {value}")
    except (KeyError, ValidationError, OSError) as e:
        config_error(str(e))
```

**Reachability note:** the filing test (`test_config_set_on_a_read_only_config_dir_is_a_clean_config_error`)
never exercises seam 2 — `ensure_auth` fails first, because `prepare_state` leaves `auth.json`
absent. Seam 2 needs its own test with an existing `auth.json` (§5.3), otherwise the widening is
untested.

### 3.5 #145 — classify egress from the registry

`gateway/models.py` (litellm-free: `dataclasses`, `typing`, `pydantic` only) gains `classify_provider`,
moved **verbatim** from `gateway/router.py:93-108`, plus the `urlparse` import it needs:

```python
def classify_provider(model: ProviderInfo) -> str:
    """Return "local" if the provider runs locally, else "cloud"."""
```

`gateway/router.py` keeps the name bound for its own three uses (`:285`, `:327`, `:904`) and for its
importers, with `from openreview_cli.gateway.models import ... classify_provider ...` (or an
`__all__` entry) and a short comment saying why the definition moved. **This is the one thing that
makes #145 possible at all**: `tui/domain/egress.py` cannot import `router.py` (module-level
`import litellm` at `router.py:11`).

`tui/domain/egress.py`:

```python
from openreview_cli.gateway.models import ProviderInfo, classify_provider
from openreview_cli.gateway.registry import load_registry


def _is_cloud(provider: str, registry: Mapping[str, ProviderInfo] | None = None) -> bool:
    """Return True when a provider is not known to run on this machine.

    Classification comes from the registry (``base_url`` / ``is_local``), which is the
    same source the gateway's tier gate uses (C7). An unresolvable provider is cloud:
    a name nobody declared cannot be asserted to be local, and the gateway fails closed
    on the same state (router.py:285,327). An empty name is not a destination.
    """
    if not provider:
        return False
    if registry is None:
        registry = load_registry()
    info = registry.get(provider)
    if info is None:
        return True
    try:
        return classify_provider(info) != "local"
    except ValueError:
        return True                       # no base_url and not local -> cloud, same as the gate
```

and `build_egress_summary` (`:62-86`) becomes:

```python
    registry = load_registry()
    has_cloud = any(_is_cloud(d.split("/", 1)[0], registry) for d in dests)
```

`_LOCAL_PREFIXES` (`:15`) is deleted. Behavioural deltas (each is the issue's intent):

| provider | today | after |
|---|---|---|
| `ollama` (registry `is_local=True`) | local | local — unchanged |
| `openai`/`anthropic`/… (registry cloud) | cloud | cloud — unchanged |
| `lmstudio`/`localai`/`vllm`/`llama-cpp` **added with `--base-url http://localhost:1234/v1`** | cloud (false positive) | **local** |
| `local` / `ollama`, **not in the registry** | local (false negative) | **cloud** |
| `bedrock`/`vertex` (registry, `base_url=None`, `is_local=False`) | cloud | cloud — unchanged (via `ValueError`) |
| `""` | `False` | `False` — unchanged |
| `Ollama`/`ollama2` (not in the registry) | cloud | cloud — unchanged |

No error code: this is a display decision.

### 3.6 #118 — a damaged index is not "not indexed"

`retrieval/storage.py:224-233` catches only `sqlite3.OperationalError` around a body whose *first*
statement is the `self.conn` property access, and `conn` (`:24-31`) is where `PRAGMA journal_mode=WAL`
(`:28`) raises a plain `sqlite3.DatabaseError: database disk image is malformed` *(measured on a real
CLI-built sparse index truncated to half: `RetrievalStorage.get_index_meta`,
`RetrievalEngine.get_index_meta` and `RetrievalEngine.retrieve` all raise it)*.

```python
    def get_index_meta(self) -> dict[str, Any] | None:
        """Read index metadata row, or None if not yet populated.

        Raises:
            IndexCorruptError: the file exists but SQLite cannot read it as a database.
        """
        try:
            cursor = self.conn.execute("SELECT * FROM index_meta")
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)
        except sqlite3.OperationalError:
            return None                     # no such table / cannot open: "not indexed" (unchanged)
        except sqlite3.DatabaseError as exc:
            raise IndexCorruptError(
                f"Index database at {self.db_path} is damaged and cannot be read: {exc}. "
                "Re-run `openreview ingest <file>` to rebuild."
            ) from exc
```

Order matters: `OperationalError` is a `DatabaseError`, so the "not indexed" case must be first. The
import (`from openreview_cli.retrieval.errors import IndexCorruptError`) is cycle-free (`errors.py`
imports nothing). `IndexCorruptError` is *not* a subclass of `ValueError`/`RuntimeError`, so nothing
that already handles `OperationalError` by accident changes.

Per caller (the deliberate split the issue asks for):

| caller | today | after |
|---|---|---|
| `retrieval/engine.py:53-58` (`RetrievalEngine.get_index_meta`) | propagates the raw error | propagates `IndexCorruptError` |
| `retrieval/engine.py:109` (inside `retrieve`) | raw error out of `retrieve` | `IndexCorruptError` → `retrieve`'s existing handler → **exit 3** (`app.py:2358-2360`) |
| `app.py:2486-2494` (`index-status`) | traceback, exit 1 | **new** `except IndexCorruptError as exc:` before the `meta is None` branch → `typer.echo(f"Error: {exc}", err=True)` + `raise typer.Exit(code=3)`; the `meta is None` "metadata not found (N bytes)" message keeps its meaning for a genuinely un-indexed file |
| `app.py:2146` (`ingest`) | broad `except Exception` → rebuild | unchanged: `IndexCorruptError` is an `Exception`, so it rebuilds exactly as today (`test_fuzz_sqlite`'s "ingest recovers" arm) |
| `retrieval/ingest.py:334` (post-ingest meta read) | propagates | **wrap** the read: on `IndexCorruptError`, fall back to the synthesised meta dict that the `meta is None` branch two lines below already builds — an ingest that has just written the index must not fail on a read of it |

`index-status` exit **3** is the same code `retrieve` already uses for the identical condition
("Index database is corrupt"). Unifying 2 / 3 / 40 / 41 is RT-003 and stays out of scope (C9).
The TUI already converts this class to `IndexCorruptError` (`tui/domain/retrieval.py:83-98`, `:117-120`)
and needs no change; its test (`tests/unit/tui/test_retrieval_domain.py::test_index_meta_reports_a_malformed_file_as_a_damaged_index`)
must stay green.

### 3.7 #116 — no egress, no lost cause, no mislabelled phase

(a) **Pin the extractor to the bundled snapshot** (preferred option 1). In
`pii/engine.py::_ensure_analyzer` (`:32-56`), before the analyzer is built:

```python
    def _ensure_analyzer(self) -> Any:
        if self._analyzer is not None:
            return self._analyzer

        # Presidio's EmailRecognizer.validate_result() calls tldextract.extract(),
        # which fetches the public suffix list over HTTPS on a cold cache with
        # timeout=None and only tolerates requests.RequestException — a blocked
        # socket (RuntimeError) escapes and takes the whole PII phase with it.
        # An empty suffix_list_urls pins resolution to the snapshot shipped in the
        # wheel: no egress, no unbounded connect, no environment dependency.
        # ``extract()`` looks TLD_EXTRACTOR up in the implementation module at call
        # time, so setting it here covers every recognizer (the same lever
        # tests/conftest.py:315 uses for the suite).
        import tldextract
        import tldextract.tldextract as _tldextract_impl

        _tldextract_impl.TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
        ...
```

*(measured: the module default is the two-network-URL `TLDExtract` with `cache_fetch_timeout=None`;
after the pin `TLD_EXTRACTOR.suffix_list_urls == ()`.)* With C10, README.md:11, README.md:127,
PRODUCT.md:18 and PRODUCT.md:65 need **no** edit. Rejected alternatives: setting
`TLDEXTRACT_CACHE_TIMEOUT` (read at import time, so it cannot be set from inside the engine; and it
bounds the stall without removing the egress), and a custom `EmailRecognizer` subclass (more code,
reaches into Presidio's registry, no benefit over the pin).

(b) **Chain the cause** in `detect_all_pages` (`:190-197`):

```python
        if failed_pages and not allow_partial:
            from openreview_cli.pii.models import PartialProcessingError

            first_page = sorted(error_messages)[0]
            raise PartialProcessingError(
                failed_pages=sorted(set(failed_pages)),
                successful_pages=successful_pages,
                error_messages=error_messages,
            ) from _first_failure            # <-- the cause survives
```

`_first_failure` is the first captured exception object, retained alongside `error_messages` in the
loop (`except Exception as exc: ...; if _first_failure is None: _first_failure = exc`). `__cause__`
then survives all the way out, and `PartialProcessingError.__str__` (`pii/models.py:146-161`) stays
page-numbers-only, so `tests/unit/test_pii_fail_closed.py:39-53` and
`tests/unit/test_bilateral_pii_strip.py:199-217` (which pin `failed_pages` / `error_messages`) are
untouched. `app.py:1227-1229` and `pipeline/adapters/strip.py:91-98` keep printing page counts —
with `--debug` the cause now reaches the log through the normal exception path. Exit code unchanged
(**9** via `PiiError`, or `CriticalStageError` from the pipeline).

(c) **Stop mislabelling the phase** at `engine.py:91`: `phase = "regex phase" if is_non_english else
"NER phase"` claims NER for a failure that came out of a *regex* recognizer's post-validation. New
rule: derive the phase from the recognizer that raised, not from the clause language:

```python
        except Exception as exc:
            # English clauses still run regex recognizers; the language of the text
            # says nothing about which recognizer failed. Custom/pattern recognizers
            # (score 1.0) run before the NLP engine, so a pattern recognizer that
            # raises is a "regex phase" failure on any language.
            phase = "regex phase" if _is_pattern_failure(exc) else "NER phase"
```

`_is_pattern_failure(exc)` is a small predicate over the recognizer identity carried by the
exception chain (`getattr(exc, "recognizer", None)` / the `PiiError`-shaped causes Presidio raises),
defaulting to `is_non_english`'s old answer when nothing matches — i.e. **the existing labels are
preserved for every case that has no better information**, and the tldextract path now says
`regex phase`, which is correct. `tests/unit/test_pii_models.py:150-160` and
`tests/integration/test_pii_error_handling.py:33-39` construct `PiiError` explicitly and are
unaffected.

### 3.8 #115 — one entity per span before placeholders exist

Root cause, re-verified *(measured on the real engine, `PiiEngine(threshold=0.7)`)*:

- `detect_on_page("Tax ID is 11-7654320")` → **two** entities on span (10,20): `TAX_ID` score 1.0
  (regex) and `DATE_TIME` score 0.85 (nlp).
- `assign_placeholders` (`placeholders.py:38-78`) groups by *prefix* (`:46 for prefix, group in
  sorted(groups.items())`), so the one value is registered twice: `DATE_1 -> "11-7654320"` and
  `TAX_ID_1 -> "11-7654320"`.
- `strip_pii` (`engine.py:316-323`) sorts `mapping.items()` by value length **descending with a
  stable sort**, so the tie keeps insertion order — alphabetical prefix order — and
  `DATE_1` replaces first. Then the losing `TAX_ID_1` finds nothing to replace, and the
  "ensure every placeholder appears" loop (`engine.py:329-332`) **appends it as a stray**:
  `Tax ID is [DATE_1] [TAX_ID_1]` *(measured, byte-exact)*.
- On the committed fixture `tests/fixtures/pii/seeded_contracts/auto/auto_contract_1.txt` the same
  mechanism produces `Tax ID is [DATE_1]`, `... is [PARTY_E]` for `REG-100001` (`PARTY_E` vs `REG_1`),
  and a trailing `[PHONE_1] [REG_1] [TAX_ID_1]` *(measured)* — the issue's reported output exactly.

Fix: resolve spans to one entity **in the engine**, immediately after `analyzer.analyze(...)` and
before `PiiEntity` construction, so the duplicate never reaches the placeholder layer:

```python
#: Structured/custom types outrank the open-vocabulary NER types when one span carries both.
#  A custom PatternRecognizer's match is evidence about the *shape* of the text; DATE_TIME /
#  ORGANIZATION / PERSON are inferences over the surrounding sentence.
_TYPE_PRIORITY: dict[str, int] = {
    "TAX_ID": 0, "REG_NUMBER": 1, "ACCT": 2, "ID_DOCUMENT": 3, "AMOUNT": 4,
    "PHONE_NUMBER": 5, "EMAIL_ADDRESS": 6,
    "PERSON": 7, "ORGANIZATION": 8, "LOCATION": 9, "DATE_TIME": 10,
}


def resolve_overlapping_spans(entities: list[PiiEntity]) -> list[PiiEntity]:
    """Keep one entity per span: highest score, then the most specific type, then stable order.

    Presidio removes duplicates only for identical (start, end, entity_type) — two recognizers
    firing on the same characters survive as two entities, which is what put two placeholders on
    one value (#115). The join key is the span, not the value: a value that legitimately occurs
    twice at different offsets keeps both detections.
    """
```

Rules, in order: sort by `(start, -score, priority, original_index)`; keep the first per
`(start, end)`; drop the rest. A second, *value*-level guard goes into `assign_placeholders`
(after `:46`): when the **same** `original_value` is claimed by two different prefixes (possible
across pages/stanzas), the lower-priority prefix's entity is dropped before placeholder numbering —
this makes the mapping 1:1 in value→placeholder, which is what makes the stray-append loop
(`engine.py:329-332`) a no-op for non-metadata entities. The loop itself stays, because metadata
entities legitimately do not appear verbatim in the clause text (`engine.py:325-328`).

**Measured effect on the FR-006 gate (C11):** on the seeded corpus the span-level
`pii_precision` moves **0.9526 (683/717) → 0.9482 (622/656)**; `pii_recall` is unchanged at 0.9640
(584); type-strict precision moves 0.7601 (545/717) → **0.7973 (523/656)**. Dedup can only *lower* a
type-agnostic precision metric — the removed detections were being credited by value. The ≥ 0.95
precision gate in `tests/integration/test_benchmark_pii_accuracy.py::test_pii_recall_above_threshold`
therefore **fails after this fix** and must be re-baselined in the same PR (§4.3), together with the
receipt. This is a consequence of fixing the defect, not a regression in detection.

> **Superseded (2026-09-28):** the projected type-strict figure **0.7973 (523/656)** was wrong; the
> implemented measurement is **0.8308 (545/656)**. The span-level 0.9482 (622/656) projection was
> correct. See the task doc's `## Superseded (2026-09-28)` note.

`benchmark/metrics_pii.py::_values_match` (`:56-83`) is **not** changed (C11). The defect is that no
*test* pins the placeholder label; §5.9 adds one.

### 3.9 #108 — populate `parent_id`

**Measured state of the tree:** every one of the six `parent_id=None` sites is real
(`clause_detector.py:128,149`, `docx_parser.py:174,207`, `discriminator.py:93`, `engine.py:554`), and
**two of them must stay `None`** (C12): `discriminator.py:85-94` synthesises a one-clause list for
`ground_claim`, and `engine.py:549-558` synthesises a one-clause list for `strip_pii_for_tier`.
Neither has a document, so neither has a parent.

**A correction to the brief (see §6.2):** the numbering levels are **not** already computed on the
parser path. `detect_clause_starts` (`clause_detector.py:50-57`) hard-codes `{"level": 1, "pattern":
"auto"}` for every match; `detect_numbering_pattern` (`:43-47`) computes real 0/1/2 levels but is
dead code outside tests; and `build_hierarchy`'s `headings` parameter (`:109`) is only a truthiness
gate — a TOC heading never sets a level. Measured on the real fixtures:

| fixture | clauses | levels today |
|---|---|---|
| `nda_with_pii.pdf` | 5 | all 0 (no numbering, no TOC — a plain 5-sentence flow) |
| `pdf/simple_contract.pdf` | 11 | all 1 (Article/Section collapsed by `detect_clause_starts`) |
| `docx/simple_contract.docx` | 11 | all 0 (no Heading styles) |
| `pdf/flat_document.pdf` | 5 | all 0 |
| `docx/flat_document.docx` | 5 | all 0 |

**Another correction:** `_NUMBERING_PATTERNS`'s dotted branch (`clause_detector.py:35`,
`r"^\s*\d+\.(?:\d+\.)*\s"`) matches only forms ending in a dot-then-space, and the bare
`Section <num>` pattern (`:33`) is tried first, so `detect_numbering_pattern("Section 1.1: X")`
returns **level 0** and `detect_numbering_pattern("1.1 Something")` returns **`None`**
*(measured)*. A naive switch to that table would produce a still-flat graph, so the table must be
corrected as part of this fix.

Design:

1. **Correct `_NUMBERING_PATTERNS`** (most specific first; the existing tests assert only
   `is not None` / `is None`, so they survive — see §4.4):

```python
_NUMBERING_PATTERNS = [
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+[IVXLCDM]+\b", 0),
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+\d+\.\d+", 1),   # before the bare form
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+\d+\b", 0),
    (r"^\s*(?:Clause|clause)\s+\d+(?:\.\d+)*", 0),
    (r"^\s*\d+(?:\.\d+)+\b", 1),                                  # before "N."
    (r"^\s*\d+\.\s", 0),
    (r"^\s*\([a-z]\)", 2),
    (r"^\s*\(\d+\)", 2),
    (r"^\s*\([ivxlcdm]+\)", 2),
]
```

2. **One shared linker** in `parsing/clause_detector.py`, expressed so a PDF's per-page
   `build_hierarchy` calls can share state across pages:

```python
def link_parent_ids(
    clauses: Sequence[Clause],
    open_levels: list[tuple[int, str]],       # caller-owned stack of (level, clause id)
    *,
    levels: Sequence[int | None] | None = None,
) -> None:
    """Set ``Clause.parent_id`` from each clause's numbering level (0 = top).

    ``levels`` supplies the level per clause (the DOCX parser passes its heading level
    where it has one; the PDF parser passes the numbering level). ``None`` is an
    unlevelled clause: it attaches to the deepest open ancestor if one is open, and
    never becomes an ancestor itself. The stack is a parameter, not a local, so the
    PDF parser's page loop can carry it across pages — a section that opens on page 3
    must parent the clauses on page 4.
    """
```

   Rules: for a levelled clause, pop while `open_levels`' top level is `>= level`, set
   `parent_id = top.id or None`, push `(level, clause.id)`. For an unlevelled clause,
   `parent_id = top.id or None` and **no push**.

3. **Wire it into the two parsers**:
   - `parsing/pdf_parser.py:165-172`: keep one `open_levels: list[tuple[int, str]] = []` outside the
     page loop; after `build_hierarchy(...)` for the page, compute
     `levels = [detect_numbering_pattern(c.text.splitlines()[0])["level"] if ... else None for c in clauses]`
     and call `link_parent_ids(clauses, open_levels, levels=levels)` before `yield`.
   - `parsing/docx_parser.py:200-207`: same call for the yielded clause, with
     `level = heading_match[0][1] if heading_match else numbering_level` — the DOCX style heading wins
     where it exists (that is the format's own statement of hierarchy), numbering is the fallback.
     The per-paragraph branch (`:173-174`) yields unlevelled clauses; keep it unlevelled.
   - `build_hierarchy`'s signature is **unchanged** (`tests/unit/test_clause_detector.py:66` calls it
     positionally), and it keeps setting `parent_id=None` — the linker overwrites it in the parser.

4. **Prices paid elsewhere (blast radius, all measured/simulated):** `chunking/splitter.py:71-91`
   turns `clause.parent_id` into `chunk.parent_chunk_id`, `:94-107` into `structural_location`, and
   `:110-113` (`_article_key`) into the short-clause *grouping key*, so populating parents changes
   chunk boundaries for numbered documents; `chunking/stream.py:43-44` propagates
   `parent_chunk_id`; `graph/builder.py:83-92` turns parents into `parent_child` edges.

Simulated outcome of this design *(measured with a throwaway script that parses each fixture, links,
then runs the real `ClauseHierarchyBuilder` → `compute_metrics` → `compute_health`)*:

| fixture | before (nodes/edges/pc) | after | density | depth | orphan | score |
|---|---|---|---|---|---|---|
| `nda_with_pii.pdf` | 5 / 0 / 0, score 98 | **5 / 0 / 0, score 98 — unchanged** | 0.0 | 1 | 0.0 | 98 |
| `pdf/simple_contract.pdf` | 11 / 0 / 0, score 98 | **11 / 8 / 8** | 0.0727 | 2 | 0.2727 | **89** |
| `docx/simple_contract.docx` | 11 / 0 / 0, score 98 | **11 / 8 / 8** | 0.0727 | 2 | 0.2727 | **89** |
| `pdf/flat_document.pdf` | 5 / 0 / 0, score 98 | 5 / 0 / 0, score 98 | 0.0 | 1 | 0.0 | 98 |
| `docx/flat_document.docx` | 5 / 0 / 0, score 98 | 5 / 0 / 0, score 98 | 0.0 | 1 | 0.0 | 98 |

> **Superseded (2026-09-28):** these projected scores predate the health-score re-scope. With
> `DEFAULT_WEIGHTS = [0.0, 0.0, 0.35, 0.40, 0.25]` a defect-free document — flat or structured —
> scores 100 by design, so the flat NDA pin and the 98s do move, and the numbered fixtures no longer
> score 89. See the task doc's `## Superseded (2026-09-28)` note.

Consequences for the pinned tests: **the `nda_with_pii.pdf` pins do not change** (§4.2 rows 7–8) —
that fixture has no hierarchy to find, and inventing one would be wrong. The graph caveat text
`tui/screens/graph.py:45-48` must be reworded because its parenthetical ("clause parsers assign no
parent section") is now false; the asserted first sentence
(`"No clause hierarchy was detected in this document"`) is kept verbatim so the screen pin survives.

Approved and **not** done (C12): re-baselining `MAX_EXPECTED_DEPTH` (`graph/health.py:11`) and
`DEFAULT_WEIGHTS` (`:16`). Residual, recorded: an unnumbered document still scores 98
(`c2 = 1 - 1/10 = 0.9` for `max_depth == 1`), i.e. the issue's "near-maximal for almost any real
document" complaint survives for documents with no numbering at all. See §6.4.

> **Superseded (2026-09-28):** the re-baseline was subsequently done — `DEFAULT_WEIGHTS` is now
> `[0.0, 0.0, 0.35, 0.40, 0.25]` and the score measures structural defects only, so a defect-free
> document scores 100 regardless of hierarchy and the "unnumbered document still scores 98" residual
> no longer holds. See the task doc's `## Superseded (2026-09-28)` note.

### 3.10 Order of operations — public entry points after the change

| function | order |
|---|---|
| `app._init` | level → drop stale owned handlers → stderr handler → `load_config` (guarded → 5) → **expire log files** → `RotatingFileHandler` → `install_on_root_handlers` → `ensure_auth` (guarded → 5) → `init_database` (guarded → 1) → `_cleanup_expired_pii` → `_refresh_model_registry` |
| `config_set` | `set_config_value` → `except (KeyError, ValidationError, OSError)` → `config_error` (5) |
| `storage.get_connection` | `connect` → **`_enable_wal` (retry on "locked")** → `foreign_keys` → row_factory |
| `index_status` | resolve path → not-exists → 2 → `RetrievalEngine.get_index_meta` → **`IndexCorruptError` → message + exit 3** → `None` → "metadata not found" + return 0 → report |
| `retrieve` | … → `engine.retrieve` → `IndexNotFoundError` → 2 / **`IndexCorruptError` → 3** (unchanged) |
| `PiiEngine.detect_on_page` | `_ensure_analyzer` (**pins TLD_EXTRACTOR**) → `analyze` → non-English filter → **`resolve_overlapping_spans`** → `PiiEntity` list |
| `PiiEngine.detect_all_pages` | per clause → `detect_on_page` → collect → on failure **`raise PartialProcessingError(...) from first_failure`** |
| `strip_pii` | detect → `assign_placeholders` (**value-unique**) → longest-first replace → metadata-only stray append |
| `PdfParser.parse` | page loop → `detect_clause_starts` → `build_hierarchy` → **`link_parent_ids(clauses, open_levels)`** → yield |
| `DocxParser.parse` | cut loop → heading level else numbering level → **`link_parent_ids`** → yield |
| `build_egress_summary` | `get_slot_configs` → dests → **`load_registry()` once** → `_is_cloud(provider, registry)` → summary |

---

## 4. Existing tests that pin the OLD behaviour (exhaustive)

### 4.1 `xfail(strict=True)` expectation nodes — remove the marker, assertions unchanged

| node | where | issue |
|---|---|---|
| `tests/chaos/test_chaos_resource_limits.py::test_config_set_on_a_read_only_config_dir_is_a_clean_config_error` | `:87` | #158 |
| `...::test_a_read_only_data_dir_is_a_clean_error` | `:130` | #157 |
| `tests/chaos/test_chaos_concurrency.py::test_a_cli_start_racing_an_open_migration_transaction_is_a_clean_error` | `:393` | #159 |
| `tests/redteam/test_redteam_egress_guard.py::test_egress_summary_does_not_claim_cloud_for_a_local_provider` | `:657` | #145 |
| `...::test_egress_summary_warns_when_a_cloud_provider_is_named_local` | `:683` | #145 |

Note for #145: both nodes patch `get_slot_configs` and `read_privacy_tier`, then call
`build_egress_summary`. With C7/C8 they hit the *real* registry via `load_registry()`; `lmstudio`
is not in the bundled registry (unresolvable → cloud → no warning ⇒ the "does-not-claim" node would
fail for the wrong reason). Both nodes must be **strengthened with a deterministic registry patch**
(`monkeypatch.setattr(egress, "load_registry", lambda: {...,"lmstudio": ProviderInfo(...is_local=True, base_url="http://localhost:1234/v1")})`)
in the same edit that removes the marker — otherwise they pass for an accidental reason (§5.5).

The #145 node at `:680` currently reads `_w5.prepare_state` + `load_registry()` and asserts the
*real* bundle's `ollama` entry; keep that reachability assertion.

### 4.2 Characterisation pins (they pass today and break when fixed)

| # | file · test | exact assertion that changes | action |
|---|---|---|---|
| 1 | `tests/chaos/test_chaos_resource_limits.py::test_config_set_on_a_read_only_config_dir_escapes_today` (`:106`) | `isinstance(result.exception, PermissionError)`, `result.output == ""` | **Invert + rename** to `..._is_a_clean_config_error_and_names_auth_json`. Keep the reachability guard (`_assert_not_writable`). New assertions: `result.exit_code == 5`, `TRACEBACK_TOKEN not in output`, `"Error" in output`, **and** `"auth.json" in output` (the old pin's value was "the PermissionError object itself"; the new pin's value is "the message names the file that could not be written" — not a byte-for-byte copy of the expectation node, which only checks a token). |
| 2 | `...::test_a_read_only_existing_database_escapes_with_no_message` (`:147`) | `isinstance(result.exception, sqlite3.OperationalError)`; `"readonly" in str(exception) or "unable to open" in ...`; `result.output == ""` | **Invert + rename** to `..._is_a_named_storage_error`. New: `result.exit_code == 1`, no traceback, `"cannot open database" in output` **and** `str(state.db_path) in output` (the distinguishing content vs the expectation node, which only requires the token `Error` and forbids a traceback). |
| 3 | `...::test_a_fresh_unwritable_data_dir_escapes_with_no_message` (`:169`) | same three conjuncts | **Invert + rename** likewise. Keep the two assertions that the expectation node does not make: `not state.db_path.exists()` ("nothing was created on a read-only tree") and `str(state.db_path) in output`. |
| 4 | `tests/chaos/test_chaos_concurrency.py::test_a_cli_start_racing_an_open_migration_transaction_crashes_locked` (`:413`) | `isinstance(result.exception, sqlite3.OperationalError)`, `"locked" in str(exception)`, `result.output == ""` | **Invert + rename** to `..._is_a_named_database_error_after_the_retry`. New: `assert_clean_database_failure(...)` **plus** "locked" named in the message **plus** the two conjuncts the expectation node does not make: `w8.user_version(state.db_path) == 0` and `not _has_client(state.db_path, "w8c-cold")` — the lock holder still holds, so the retry must have exhausted *and* nothing must have been written. (Do **not** assert success: the test's holder keeps the write transaction open for the whole `invoke`; the retry-bounded failure is the correct outcome, and the `finally: holder.rollback()` then releases.) |
| 5 | `tests/redteam/test_redteam_egress_guard.py::test_is_cloud_classifies_by_name_and_cannot_see_base_url` (`:635`) | `assert egress._is_cloud("local") is False` (`:642`) | **Invert the one line + rename + rewrite docstring.** `_is_cloud("ollama") is False` survives (registry `is_local=True`); `_is_cloud("local") is True` after the fix — which is precisely the issue's false-negative. The `for name in ("lmstudio","localai","vllm","llama-cpp") → cloud_warning is True` loop **survives** (unresolvable → cloud); add a registry-patched twin proving the *false positive* is fixed (§5.5). Rename to `..._classifies_from_the_registry_not_from_the_name`. |
| 6 | `tests/exploratory/test_explore_egress_counter.py::test_is_cloud_matches_only_literal_ollama_and_local` (`:30`) | `assert eg._is_cloud("local") is False` (`:36`) | **Invert that line** (`is True`, with a comment saying the name is no longer evidence) and **rename** to `test_is_cloud_reads_the_registry_not_a_name_allowlist`; `_is_cloud("")` stays `False` (C7's empty-name rule) and the `("openai","anthropic","localai","lmstudio","vllm","Ollama","ollama2") → True` loop stays. Add the registry patch to the test (or to the module's `_patch` helper) so it stops reading the developer's `~/.config/openreview/models.json`. |
| 7 | `tests/exploratory/test_explore_egress_counter.py::test_local_vs_cloud_warning_requires_pii_disabled` (`:55`) | the `("local", "llama", False, "local/llama")` row (`:50`) now yields `cloud_warning is True` | **Replace the row**: either `("local","llama",True,"local/llama")` (a registry-absent name is cloud — honest, but the row then documents a name nobody declares) or — preferred — swap it for `("lmstudio","m",False,"lmstudio/m")` **with a patched registry declaring `lmstudio` local**, which is the positive twin of pin 5 and the case the issue actually names. Keep the `ollama` row (unchanged) and the two cloud rows. |
| 8 | `tests/unit/test_tui_graph_domain.py::test_real_fixture_reports_its_measured_numbers` (`:27`) | — | **No change (measured).** `nda_with_pii.pdf` has no numbering and no TOC; every clause stays level 0 with `parent_id=None`, so 5 nodes / 0 edges / depth 1 / orphan 0.0 / score 98 all hold. Do not "fix" the numbers; if the Plan stage finds this test red, the linker is wrong. |
| 9 | `tests/integration/tui/test_graph_screen.py::test_real_pdf_renders_real_metrics_and_score` (`:53`) | — | **No change (measured)**, same reason. |
| 10 | `tests/integration/tui/test_graph_screen.py::test_real_pdf_shows_the_no_hierarchy_caveat` (`:73`) | asserts the substring `"No clause hierarchy was detected in this document"`; the test's docstring says "No clause parser populates parent_id" | **Docstring edit only** (the assertion survives because the asserted sentence is kept verbatim in `tui/screens/graph.py:46`). The caveat's parenthetical `"(clause parsers assign no parent section)"` (`graph.py:47`) is now **false for numbered documents** and must be reworded to describe the *document* ("this document declares no section hierarchy"). |
| 11 | `tests/integration/test_stream_clauses.py::test_parent_id_chain_integrity` (`:66`) | none — the loop body is **vacuous today** (`parent_id` is never set, so `if clause.parent_id is not None` never fires) | **Strengthen, do not rename.** Add `assert any(c.parent_id for c in clauses)` for `simple_contract.pdf`/`.docx` (measured: 8 non-None parents each) and keep the integrity loop; add a flat-fixture arm asserting **no** clause has a parent. The name already describes the new behaviour. |
| 12 | `tests/fuzz/test_fuzz_sqlite.py::test_damaged_index_file_is_not_misreported` (`:173`) | `with pytest.raises(sqlite3.DatabaseError): RetrievalStorage(path).get_index_meta()` | **Invert + rename** to `test_damaged_index_file_is_reported_as_index_corrupt`: `pytest.raises(IndexCorruptError)`, keep the docstring's reference to #118 (this test *is* the filed issue's repro), and assert `isinstance(exc.value, RetrievalError)`. **This pin is not in the brief's list and is the only automated repro #118 has.** |
| 13 | `tests/integration/test_benchmark_pii_accuracy.py::test_pii_recall_above_threshold` (`:59`) | `assert precision.value >= 0.95` | **Re-baseline (this is the forced case)**: measured 0.9526 → 0.9482 after resolution, so the assertion must move to the new value with an explicit note that span-level precision *cannot* stay at 0.95 once the wrong-label duplicates it was crediting are removed, and that recall is unchanged (0.9640, still ≥ 0.95). Preferred form: keep a ≥0.94 gate **and** add a `pii_precision_type_strict` assertion (measured 0.7973) so the suite stops rewarding wrong labels. Flag this in the PR description as a gate change, not a silent slackening. |
| 14 | `docs/benchmarks/results/pii-accuracy.json` + `docs/BENCHMARKS.md` (the `## PII accuracy (measured 50 seeded contracts)` section) | pinned by `tests/unit/test_benchmark_receipts.py::test_pii_accuracy_per_type_numerators_match_the_receipt` (`:607`) and `::test_pii_accuracy_overall_recall_matches_the_receipt` (`:616`), plus `::test_every_receipt_pins_its_producing_content` (`:382`) over the `metrics_pii.py` sha256 | **Re-measure and update the receipt + the page** (Precision `95.3% | 683 / 717` → `94.8% | 622 / 656`, F1, the `n` fields); the ORGANIZATION per-type cell and the Recall cell (96.4%) do **not** change, so `:607`/`:616` keep passing. The `metrics_pii.py` provenance sha is unchanged because C11 leaves that file alone — verify before touching the receipt. |

> **Superseded (2026-09-28) for rows 8–10:** after the health-score re-scope a defect-free document
> scores 100, so the flat NDA pin (rows 8–9) is now **100/100**, not the 98 recorded here, and the
> row-10 caveat docstring/copy was reworded to match. See the task doc's
> `## Superseded (2026-09-28)` note.

### 4.3 Tests that are neither xfail nor "old-behaviour" pins but still need an edit

| file · test | why | action |
|---|---|---|
| `tests/chaos/test_chaos_resource_limits.py` / `tests/chaos/test_chaos_concurrency.py` (both `assert_clean_failure` / `assert_clean_database_failure` oracles) | they are the anti-vacuity anchors | **No change** — `tests/chaos/test_chaos_resource_limits.py:299-341` (negative controls) and `:455-490` in the concurrency file must stay untouched and green. |
| `tests/unit/test_cli_config.py::test_repeated_invokes_do_not_accumulate_file_handlers` (`:157`) | it counts `isinstance(h, logging.FileHandler)` on the root logger | **No change**: `RotatingFileHandler` **is** a `logging.FileHandler` subclass, and the `_openreview_owned` replacement logic is untouched. Verify explicitly in the plan's GREEN step — this is the one test that would catch a handler that forgets the ownership marker. |
| `tests/unit/test_api_key_redaction_e2e.py` (`:121`, `:185`) | reads `openreview.log` and asserts the key is absent / the redacted tail is present | **No change**, but it is the guard for the #161 ordering constraint: it fails if the `RotatingFileHandler` is created after `install_on_root_handlers()`. |
| `tests/redteam/test_redaction_ordering.py` (whole file) | documents the "handler added after install leaks" hole | **No change.** Its `:95` docstring ("the product's own `_init` avoids it by installing last (`app.py:246-254`)") gains a second stale line number; fix the reference, not the assertions. |
| `tests/unit/tui/test_egress_summary.py` (all 10 tests) | they patch `get_slot_configs`/`read_privacy_tier` but not the registry | **No change to assertions** (providers used are `ollama`, `openai`, `anthropic`, `""` — all resolvable/skipped), but **add the registry patch** to a shared helper so the developer's `~/.config/openreview/models.json` cannot change the result (§1.3). |
| `tests/redteam/_w6_probe.py:91` (`LOG_FILENAME`), `tests/redteam/_w5_probe.py:20`, `tests/chaos/_w8_probe.py:104-107` | each builds its own `log_dir` and (W6) its own handlers | **No change** — none of them construct the product's handler; the expiry pass only ever touches `log_dir/openreview.log*` inside the per-test `tmp_path`. |
| `tests/unit/test_grounding_models.py`, `tests/unit/test_grounding_discriminator.py`, `tests/integration/test_grounding_pipeline.py` | #160 changes only a log call's arguments | **No change** — no test in the tree uses `caplog` on the grounding logger (`grep caplog tests/` → retrieval/gateway/pii only). |
| `tests/unit/test_pii_recognizers.py` (`:76`, `:85`) | asserts the TAX_ID/PHONE patterns match `REG-100001` / `555-0101` | **No change** — the recognizers are not edited; only their *result resolution* is. |
| `tests/unit/test_pii_models.py:150-160`, `tests/integration/test_pii_error_handling.py:33-39` | construct `PiiError` with explicit `phase=` strings | **No change** (they never call the engine). |

### 4.4 Verified GREEN after the change (no edit — recorded so the Plan stage can skip them)

- `tests/unit/test_retrieval_storage.py::TestIndexMeta::test_get_index_meta_returns_none_when_empty`
  (`:263`) and `::test_get_index_meta_returns_fields` (`:267`): an empty schema raises
  `OperationalError("no such table")` → still `None`. *(C9's ordering is what keeps these green.)*
- `tests/unit/test_retrieval_engine.py::test_get_index_meta_returns_none_when_no_db` (`:212`): the
  file is created by `sqlite3.connect`, then "no such table" → `None`.
- `tests/fuzz/test_fuzz_sqlite.py::test_corrupt_shared_database_is_a_clean_error` (`:138`): a
  *malformed shared* db is already routed by the `except sqlite3.DatabaseError` branch; deleting the
  `OperationalError` re-raise does not disturb it.
- `tests/fuzz/test_fuzz_sqlite.py::test_retrieval_marked_corrupt_index_is_index_corrupt` (`:157`) and
  `::test_retrieval_missing_index_is_index_not_found` (`:147`): `index_status == 'corrupt'` is a
  *row value*, not an unreadable file — untouched.
- `tests/unit/tui/test_retrieval_domain.py::test_index_meta_reports_a_malformed_file_as_a_damaged_index`
  (`:232`) and `::test_search_reports_a_malformed_file_as_a_damaged_index` (`:245`): the TUI catches
  `sqlite3.DatabaseError` before the storage layer's new raise is observable — still green.
- `tests/exploratory/test_explore_retrieval_group.py::test_retrieval_registry_codes_are_unreachable_from_the_cli`
  (`:160`): green iff `index-status` uses a literal `typer.Exit(code=3)` and does **not** import
  `retrieval_error`/`EXIT_RETRIEVAL_*` into `app.py` (C9). Do not "tidy" the code to the registry.
- `tests/unit/test_clause_detector.py::test_detect_numbering_pattern_*` (`:38-50`) and
  `tests/unit/test_pdf_parser.py` (`:60-82`): they assert only `is not None` / `is None`, so the
  corrected table passes. Verify — this is the assumption the #108 change rests on.
- `tests/unit/test_clause_detector.py::test_build_hierarchy_*` (`:66`, `:70`): `build_hierarchy`'s
  signature and level behaviour are unchanged.
- `tests/unit/test_graph_builder.py` (`:34`, `:64`, `:84`, `:160`): builds `Clause` objects with
  explicit `parent_id` — unaffected by parser-side linking.
- `tests/unit/test_chunking_stream.py::test_hierarchy_preserved_in_stream` (`:218`) and
  `tests/unit/test_chunking_splitter.py` (`:172`): manual clauses.
- `tests/unit/test_pii_fail_closed.py` (`:39-53`), `tests/unit/test_bilateral_pii_strip.py`
  (`:199-217`), `tests/unit/test_pii_engine.py::test_is_available_logs_warning_on_failure` (`:294`):
  the `from exc` change adds `__cause__` and changes nothing these assert.
- `tests/integration/test_pii_strip_command.py::test_email_detection_needs_no_suffix_list_network`
  (`:53`): still green (the session fixture and the engine's own pin agree), and §5.8 covers the gap
  it leaves.
- `tests/unit/test_log_redaction_handlers.py`, `tests/redteam/test_redteam_gateway_setup.py:45`,
  `tests/unit/test_output_write_errors.py:158`: handler-count/`get_log_dir`-patch based; unaffected.

---

## 5. New tests to add (one per contract; the TDD anchors)

1. **#161 / C1 — the log family is bounded.** Drive `_init` twice against a temp `log_dir`, writing
   `_LOG_MAX_BYTES`+ bytes through the installed handler, then assert a `openreview.log.1` exists and
   the active file is under `_LOG_MAX_BYTES`. Costs ~10 MiB of writes: build the handler through
   `_init` (not by re-constructing it) and write with a *raising-free* logger.
2. **#161 / C2 — retention is applied.** Unit test `_log_retention_days` for `(30,30)→30`,
   `(7,30)→7`, `(30,7)→7`, `(0,30)`/missing keys → 30 (the loader's `ge=1` means 0 cannot come from
   config; the helper must still not raise). Unit test `_expire_log_files`: with
   `openreview.log` (`mtime = now`) + `openreview.log.1` (`mtime = now - 40d`) and a 30-day window,
   exactly `.1` is unlinked; with both fresh, nothing is; with `openreview.log` stale (now - 40d),
   it too is unlinked. Also assert a **non-matching** sibling (e.g. `other.log`) is never touched.
3. **#158 / C6 — both seams.** (a) `_init` path: `test_config_set_on_a_read_only_config_dir_is_a_clean_config_error` (§4.1, un-marked) now covers the auth write. (b) `config set` path: seed an
   **existing** `auth.json`, chmod the config dir 0o500, invoke `config set privacy.tier maximum`,
   assert exit 5, `"Config error"` in output, no traceback, and that `config.yml` is unmodified.
4. **#157/#159 / C4, C5 — retry then named failure.** (a) Unit test `_enable_wal`: with a second
   connection holding `BEGIN IMMEDIATE` on a non-WAL file, `sqlite3.connect(...)` + `_enable_wal`
   blocks, and after the holder releases the pragma succeeds (run it in a thread with a bounded
   join, or hold the lock for ~0.3 s so one retry suffices). (b) Assert the retry count via a
   `time.sleep` spy (5 attempts max, non-"locked" errors raise immediately with **no** sleep).
   (c) A "readonly"/"unable to open" error is asserted to raise on the **first** attempt.
5. **#145 / C7, C8 — registry-classified egress.** Unit test with an injected registry:
   `{"custom-local": ProviderInfo(name="custom-local", base_url="http://localhost:1234/v1")}` →
   `_is_cloud("custom-local", registry) is False`; `{"remote-named-local": ProviderInfo(name="remote-named-local", base_url="https://api.example.com/v1")}` → `True`; `{}` (nothing declared) →
   `True` for any non-empty name; `""` → `False`. Plus one `build_egress_summary` test asserting
   `load_registry` is called **once** for a two-destination summary (spy counter) — the C8 contract.
6. **#118 / C9 — the three states are three answers.** `RetrievalStorage.get_index_meta` on:
   a fresh empty db → `None`; a truncated real index (build one with `ingest_document`, as §3.6's
   probe does) → `IndexCorruptError` mentioning the path; the same file through
   `RetrievalEngine.get_index_meta` → `IndexCorruptError`. Plus a CLI test: `index-status` on a
   truncated index → exit **3**, `"damaged"`/`"corrupt"` in output, no traceback; `retrieve` → exit
   **3**; `ingest` → exit **0** and a rebuilt index (the recovery arm must not regress).
7. **#116 / C10 — the engine pins the snapshot, independent of the fixture.** In an integration test:
   `monkeypatch.setattr(tldextract_impl, "TLD_EXTRACTOR", tldextract.TLDExtract())` (network
   default), construct a **fresh** `PiiEngine`, call `_ensure_analyzer()`, then assert
   `tldextract_impl.TLD_EXTRACTOR.suffix_list_urls == ()`; then spy
   `tldextract.cache.cached_fetch_url` to raise and assert `detect_on_page("Email: a@b.com")` still
   returns an `EMAIL_ADDRESS` (the strip works with **no** fetch). Cost: one spaCy load.
8. **#116 — the cause survives and the phase is honest.** `detect_all_pages` over one clause whose
   `detect_on_page` raises a sentinel `RuntimeError`; assert `pytest.raises(PartialProcessingError)`
   and `exc.value.__cause__ is the sentinel`. Separately, monkeypatch the analyzer so a *pattern*
   recognizer raises on English text and assert the `PiiError.phase == "regex phase"` (the old code
   said "NER phase"); and that an NLP-engine failure still reports "NER phase".
9. **#115 / C11 — one span, one placeholder, the right label.** Using the shared `pii_engine`
   fixture: `strip_pii([clause("Tax ID is 11-7654320")], …)` → `stripped_text == "Tax ID is [TAX_ID_1]"`
   and `mapping == {"TAX_ID_1": "11-7654320"}` (no `DATE_1`, no appended stray). Plus a
   `detect_on_page` unit assertion: exactly one entity on span (10,20) and its type is `TAX_ID`.
   Plus the fixture-level regression: through the public `strip_pii` API on
   `auto/auto_contract_1.txt`, `"Tax ID is [TAX_ID_1]"` appears, `"[DATE_1]"` does not, and the
   output carries no stray trailing placeholder from a duplicate (`[REG_1]`/`[PHONE_1]` survive as
   *substitution sites*, not as appended strays). Plus a `_values_match`-untouched guard: assert
   `tests/unit/test_benchmark_metrics_pii.py`'s predicate is unchanged (no edit to that module).
10. **#108 / C12 — parent ids are real and consistent.** `parse_document(simple_contract.pdf)` →
   ≥1 clause has `parent_id is not None`; every `parent_id` is in the id set; the Article clause is
   the parent of its Sections (`clauses[1].parent_id == clauses[0].id`); `flat_document.*` and
   `nda_with_pii.pdf` → every `parent_id is None`. Plus the graph consequence:
   `graph_summary_via_tui(simple_contract.pdf)` → `parent_child_edge_count == 8`,
   `metrics.max_depth == 2`, `score == 89` (the new pin for the *numbered* fixture, which no test
   pins today). Plus the chunking consequence (the ripple): the first chunk of a Section clause has
   `parent_chunk_id == ` the first chunk of its Article clause, and its `structural_location` starts
   with the Article's id — pinning `chunking/splitter.py:71-91`'s new output.
11. **#160 / C3 — no clause text in the log.** `caplog.at_level("WARNING", logger="openreview_cli.grounding.models")`
   around a strict-mode `merge_into` with an UNGROUNDED verdict whose `clause_text` is a long unique
   canary: assert the record contains `"Claim #0"` and the verdict, and that the canary and
   `clause_text[:40]` are **absent** from `caplog.text`. Negative control: assert the same canary
   *is* found when the logger is swapped for a recording handler (proving the assertion can fail).

---

## 6. Risks, what could not be verified, blast radius

### 6.1 The issue bodies' line numbers are stale

Every anchor in all ten issues has drifted. Verified: `_init`'s handler is `app.py:253`, not `:232`;
`init_database` is `app.py:278`, not `:264`; `ensure_auth` is `app.py:272`, not `:260`;
`config_set`'s handler is `app.py:514`, not `:493`; `index-status`'s read is `app.py:2488`, not
`:2455`; `retrieve`'s `engine.retrieve` is `app.py:2354`, not `:2321`; the ingest probe is
`app.py:2146`. `src/openreview_cli/retrieval/storage.py:28,224-233` are still exact — but
**`src/openreview_cli/storage/database.py:12` is wrong**: the pragma that #157/#159 name is at
`storage/database.py:15` (the date of the drift is unknown; a `git log -L` on that line would say).
`grounding/models.py`'s warning
is `:105-112` with `clause_text[:40]` at `:109` (issue says `:106-112`). `_is_cloud` is
`tui/domain/egress.py:48-50` with `_LOCAL_PREFIXES` at `:15` (issue: `:15`/`:48-50` — exact).
`tests/conftest.py`'s tldextract fixture is `:286-315` (brief says `:285`). **The issue text's
references must not be copied into the PR description without re-checking.**

### 6.2 The brief's #108 premise is wrong in two ways

1. **"the numbering levels the detector already computes" do not exist on any production path.**
   `detect_clause_starts` hard-codes `level=1` for every match (`clause_detector.py:56`);
   `detect_numbering_pattern`'s real 0/1/2 levels are dead code outside tests;
   `build_hierarchy`'s `headings` argument never sets a level. Measured: `simple_contract.pdf`
   parses to 11 clauses all at level 1; `docx/simple_contract.docx` to 11 clauses all at level 0.
   The fix therefore has to *create* the level source (§3.9 item 1), not "use" it.
2. **`_NUMBERING_PATTERNS` is itself broken for `N.N`**: `"Section 1.1: X"` → level **0** (the bare
   `Section` pattern shadows the dotted one) and `"1.1 Something"` → **`None`** (the dotted pattern
   requires a trailing dot). Using it unmodified would produce a still-flat graph — the defect would
   survive the fix.
3. **The two "pinned tests that will change" do not.** `tests/unit/test_tui_graph_domain.py:27-40`
   and `tests/integration/tui/test_graph_screen.py:59-81` pin `nda_with_pii.pdf`, which has **no
   numbering and no TOC** — 5 plain sentences. Measured: after linking it is still 5 nodes / 0 edges /
   depth 1 / score 98. If those two tests go red, the linker is wrong. `test_parent_id_chain_integrity`
   is a *third* case: it is vacuous today and becomes meaningful, so it needs strengthening rather
   than updating.

### 6.3 #108's blast radius (the largest in this batch)

Populating `parent_id` changes three downstream behaviours that have **no** existing pin, so the
change is invisible to the current suite unless §5.10 lands:

- **Chunk boundaries.** `chunking/splitter.py:110-113`'s `_article_key` returns `parent_id or id`,
  and `group_short_clauses` (`:28-68`) groups short clauses by that key. Numbered documents will now
  group per section instead of per clause → different chunk counts, texts and offsets →
  `chunk.parent_chunk_id` becomes non-None (`chunking/stream.py:43-44`) and `structural_location`
  gains ancestors (`:94-107`). Nothing currently asserts either for a real fixture
  (`tests/integration/test_chunking_cli.py` asserts only `len(chunks) > 0`).
- **Retrieval.** Ingested chunks carry `heading_chain`/`parent_chunk_id`; existing **ndax fixtures
  and existing indexes do not change** (they are pre-chunked JSON, `app.py:1829`), so no re-index is
  forced. New ingests of numbered documents will store different `heading_chain` values.
- **TUI/CLI graph screens.** Only the numbered fixtures move (98 → 89); the caveat text needs the
  rewording in §4.2 row 10.
- **`.ndax` round-trips.** `parse`/`chunk --format json` output now carries `parent_id`; the JSON
  schema is unchanged (`parsing/stream.py:153`), so it is additive and safe.
- **Not done (C12):** `MAX_EXPECTED_DEPTH`/`DEFAULT_WEIGHTS` re-baselining. Residual documented: an
  unnumbered document still scores 98, so the issue's headline complaint ("the score is near-maximal
  for almost any real document") is only fixed for documents that *have* numbering. **Decision point
  for the Plan stage:** the issue lists the re-baseline as item 3; the approved scope excludes it, so
  it must either be filed as a follow-up issue or accepted in writing in the PR.

### 6.4 #116 — residual and unverifiable items

- The pin **mutates a library global** (`tldextract.tldextract.TLD_EXTRACTOR`). It is process-wide:
  any other consumer of `tldextract` in the same process loses the network list. Nothing else in
  `src/` imports tldextract (`grep` → only Presidio's recognizer, indirectly), so the blast radius is
  Presidio's email validation only. A library bump that moves `TLD_EXTRACTOR` out of the
  implementation module would silently stop pinning: the §5.7 test fails loudly in that case
  (fail-closed).
- **The blackhole/stall behaviour was NOT measured.** The issue's author states the same: measuring a
  DROP rule needs host-level firewall control. The design removes the fetch entirely, so the stall is
  moot for the product, but "minutes of stall" remains an inference, not a measurement.
- The **`cache_fetch_timeout=None`** and **two-URL default** facts were verified in the installed
  `tldextract 5.3.1` (signature + module default), not in `cache.py:216`'s `session.get` line number
  the issue cites.
- `_tldextract_impl.TLD_EXTRACTOR` is set **inside** `_ensure_analyzer`, which is called lazily. A
  code path that constructs a Presidio analyzer **without** going through `PiiEngine` (external
  embedders of `pii.recognizers.get_custom_recognizers`) is not covered. No such caller exists in
  `src/`.

### 6.5 #160 and #115 — accepted residuals

- #160 removes clause text but keeps `result.reason`. The reason is produced by the grounding
  discriminator and is not derived from the clause body by construction, but a model *could* echo a
  fragment of it. Not measurable without a live provider call; if it matters, route `result.reason`
  through `gateway/redaction.py::redact_text` (which redacts credentials, not clause text — so this
  would be cosmetic, not protective). Decision point, flagged rather than silently assumed.
- #160's fix has **no existing pin**; a regression (someone re-adding `clause_text`) would only be
  caught by §5.11. That is the whole reason to add it.
- #115's `_TYPE_PRIORITY` table is a *policy* choice (structured > NER). An alternative policy
  ("always the higher score, ties by first-seen") flips `REG-100001` from `REG_1` (score 1.0) to
  `PARTY_E` (spaCy score) only if the NER score exceeds 1.0, which cannot happen — so for the filed
  cases both policies agree. The table exists for equal-score ties, where the more specific type is
  the defensible winner. Recorded so the choice is visible.
- Dedup changes the **count** of `result.entities` on the benchmark path (717 → 656 detections), which
  feeds `pii_throughput.json`'s "395 entities" style numbers only if a corresponding receipt is
  re-generated. `tests/unit/test_benchmark_receipts.py::test_pii_throughput_numbers_match_the_receipt`
  (`:446`) reads a *different* receipt and is unaffected — verify.

### 6.6 #157/#159 — what the retry does and does not cover

- The retry covers `PRAGMA journal_mode=WAL` only. A writer that holds the lock for longer than the
  ~2 s budget still fails — by design (C4): a CLI must not hang indefinitely behind another process,
  and the failure is now named and exit-1 clean.
- `app.py:2703-2709` (`graph_build`) calls `init_database` directly, outside `_init`; it inherits the
  retry (in `get_connection`) but keeps its own error handling. Not in scope, unchanged.
- The tests that pin the *organic* multi-process case
  (`tests/chaos/test_chaos_concurrency.py::test_four_cli_processes_write_one_database_without_loss`)
  are green today and must stay green — the retry makes them strictly more robust.
- 2 s of added latency in the worst case for every cold start; the retry is entered **only** on
  `"locked"`, so a healthy start pays zero.

### 6.7 #118 — the split's one loose end

`index-status`'s new exit 3 collides numerically with `EXIT_NOT_FOUND = 3` (`errors.py:16`) even
though the *message* ("Index database is corrupt") disambiguates. `retrieve` has had the same
collision since `app.py:2360`. Fixing the collision is RT-003 (`test_retrieval_registry_codes_are_unreachable_from_the_cli`),
whose own `xfail` repro would need reversing if this PR touched it. **Do not fix it here**; note it in
the PR body so the reviewer does not read exit 3 as a new contract.

### 6.8 What could not be verified

- The multi-minute blackhole stall for #116 (§6.4) and the full end-to-end `index-status`/`retrieve`
  CLI traceback output (the in-process reproduction gives the same exception class and empty output;
  the Rich traceback rendering was not re-run).
- Whether any **test outside this repo's tree** (e.g. an external CI job) depends on
  `tui.domain.egress._LOCAL_PREFIXES` or on `openreview.log` being a single unbounded file. Both are
  deletions/behaviour changes; `_LOCAL_PREFIXES` has no in-tree consumer besides `_is_cloud`
  (`grep`), and the log path/name are unchanged.
- The exact `retry_delay` that makes the *organic* 4-process cold-start race resolve reliably. The
  budget (5 attempts, 0.2 s linear backoff) is a judgement call; §5.4(c) pins the *shape*, not the
  race outcome. If the Plan stage wants evidence, the existing 4-process test is the place to raise
  the concurrency for a one-off measurement.
- The `pii_precision` re-baseline numbers were measured with `detect_on_page` directly (no
  overlap-buffer shifting), which reproduces the issue's own 0.9526/0.7601 exactly — but the
  engine's full `detect_all_pages` path was not re-measured. Re-measure in the Plan stage's GREEN
  step before editing the receipt.
