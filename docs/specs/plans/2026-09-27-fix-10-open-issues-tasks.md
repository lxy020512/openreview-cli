# Fix 10 open issues — task breakdown

Design: [2026-09-27-fix-10-open-issues-design.md](./2026-09-27-fix-10-open-issues-design.md).
Branch: `fix/remaining-open-issues` (base `30ffdc6`). One PR, referencing #161 #160 #159 #158 #157 #145 #118 #116 #115 #108.

Five cohesive tasks. Each is implemented by one fresh sub-agent (TDD: run the repro first), then
independently reviewed by a second fresh sub-agent. The Lead runs the full-suite QC after all five.

## Lead arbitration (deviations from the design doc)

- **#115 — fix at the placeholder layer only (design §3.8's *secondary* guard).** The design's primary
  fix (a span-level dedup inside `detect_on_page`) lowers the FR-006 span precision 0.9526 → 0.9482,
  which forces re-baselining the ≥0.95 gate and two receipts. That is a *softened benchmark gate* to
  fix a labelling defect. The same defect is fully fixed one layer down: when two recognizers claim the
  same value with different types, `assign_placeholders` must emit ONE placeholder, from the most
  specific type. `detect_on_page` and `benchmark/metrics_pii.py` are then untouched, so the metric,
  the gate and the receipts stay as they are. Redaction is unaffected because `strip_pii` replaces
  every occurrence of a value globally.
- Everything else follows the design doc as written.

## Superseded (2026-09-28)

**#115 — the arbitration above is superseded.** The owner approved the opposite: overlapping spans are
deduplicated in the engine (`detect_on_page`), the type-agnostic FR-006 span predicate is kept, and
`benchmark/metrics_pii.py` gains a `pii_precision_type_strict` metric. Span-level precision moves
0.9526 (683/717) → **0.9482 (622/656)** against FR-009's ≥0.95 target, so the span-level gate in
`tests/integration/test_benchmark_pii_accuracy.py` is re-baselined to **0.945** with
`pii_precision_type_strict ≥ 0.82` as the compensating label signal (type-strict measures 0.8308,
545/656; recall 0.9640; F1 0.956). The placeholder layer is no longer the fix site.

## Task A — CLI startup + logging (#157, #158, #159, #161, #160)

Files: `src/openreview_cli/app.py`, `src/openreview_cli/storage/database.py`,
`src/openreview_cli/config/auth.py` (unchanged, referenced), `src/openreview_cli/grounding/models.py`.

1. `storage/database.py`: `get_connection` calls a new `_enable_wal(conn)` that retries only
   `"locked"` (5 attempts, 0.2s linear backoff); non-lock errors raise immediately.
2. `app.py::_init`: delete the `except sqlite3.OperationalError: raise` branch so every
   `sqlite3.DatabaseError` takes `fail(f"cannot open database {db_path}: ...", EXIT_USER_ERROR)`.
3. `app.py::_init`: wrap `ensure_auth(config_dir)` in `except OSError` → `config_error(...)`.
4. `app.py::config_set`: widen the handler to `(KeyError, ValidationError, OSError)`.
5. `app.py`: `logging.handlers.RotatingFileHandler(maxBytes=10MiB, backupCount=5)` instead of
   `logging.FileHandler`; add `_log_retention_days(config)` = `min(privacy.log_ttl_days,
   storage.logs_keep_days)` and `_expire_log_files(log_dir, days)` (unlink `openreview.log*` with
   `mtime < now - days*86400`, best-effort). Order: stderr handler → load_config (guarded) → expire →
   rotating file handler → `install_on_root_handlers()` LAST.
6. `grounding/models.py`: drop `assessment.clause_text[:40]` from the strict-mode warning.

RED first: `uv run pytest --runxfail -q tests/chaos/test_chaos_resource_limits.py
tests/chaos/test_chaos_concurrency.py -k "readonly or read_only or racing"`.
GREEN: same nodes with the 3 `xfail` markers removed and the 4 characterisations inverted+renamed
(design §4.2 rows 1–4); `uv run pytest -q tests/chaos tests/unit/test_cli_config.py
tests/unit/test_log_redaction_handlers.py tests/unit/test_api_key_redaction_e2e.py
tests/redteam/test_redaction_ordering.py`.

## Task B — TUI egress classification (#145)

Files: `src/openreview_cli/gateway/models.py`, `src/openreview_cli/gateway/router.py`,
`src/openreview_cli/tui/domain/egress.py`, `tests/redteam/test_redteam_egress_guard.py`,
`tests/exploratory/test_explore_egress_counter.py`, `tests/unit/tui/test_egress_summary.py`.

1. Move `classify_provider` verbatim from `router.py` to `models.py` (litellm-free), add the
   `urlparse` import; re-export from `router.py` so existing imports keep working.
2. `egress.py`: delete `_LOCAL_PREFIXES`; `_is_cloud(provider, registry=None)` classifies from the
   registry (`classify_provider`), unresolvable/empty name → cloud (empty string → False).
   `build_egress_summary` loads the registry once and passes it in.
3. Remove the two `xfail` markers (§4.1 rows 4–5) and **add a deterministic registry patch** to them
   (`monkeypatch.setattr(egress, "load_registry", ...)`) — `lmstudio` is not in the bundled registry,
   so without the patch the false-positive node would pass for the wrong reason.
4. Invert/rename the two name-allowlist pins (§4.2 rows 5–6) and swap the `("local","llama",...)`
   parametrize row (§4.2 row 7). Add the registry patch so no test reads the developer's real
   `~/.config/openreview/models.json`.

GREEN: `uv run pytest -q tests/redteam/test_redteam_egress_guard.py
tests/exploratory/test_explore_egress_counter.py tests/unit/tui/test_egress_summary.py
tests/unit/test_gateway_router.py tests/unit/test_gateway_tier_enforcement.py`.

## Task C — damaged index (#118)

Files: `src/openreview_cli/retrieval/storage.py`, `src/openreview_cli/retrieval/ingest.py`,
`src/openreview_cli/app.py`, `tests/fuzz/test_fuzz_sqlite.py`.

1. `RetrievalStorage.get_index_meta`: keep `except sqlite3.OperationalError: return None` FIRST (the
   "no such table" case), then `except sqlite3.DatabaseError as exc: raise IndexCorruptError(...)`.
2. `index_status` (`app.py`): `except IndexCorruptError` → `Error: ...`, exit **3** (literal code, do
   NOT import the retrieval registry — that is RT-003, out of scope).
3. `retrieval/ingest.py:334`: wrap the post-ingest meta read; on `IndexCorruptError` use the
   synthesised meta that the `meta is None` branch already builds.
4. Invert+rename the existing pin `tests/fuzz/test_fuzz_sqlite.py::test_damaged_index_file_is_not_misreported`
   → expects `IndexCorruptError` (design §4.2 row 12). Add the CLI/engine tests (design §5.6).

GREEN: `uv run pytest -q tests/fuzz/test_fuzz_sqlite.py tests/unit/test_retrieval_storage.py
tests/unit/test_retrieval_engine.py tests/unit/tui/test_retrieval_domain.py
tests/exploratory/test_explore_retrieval_group.py tests/integration/test_retrieval_cli.py`.

## Task D — PII egress + diagnosability (#116) and placeholder type (#115)

Files: `src/openreview_cli/pii/engine.py`, `src/openreview_cli/pii/placeholders.py`.

1. `_ensure_analyzer`: pin `tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())`
   before building the analyzer (same lever as `tests/conftest.py`).
2. `detect_all_pages`: retain the first captured exception and `raise PartialProcessingError(...) from first_failure`.
3. `detect_on_page`: stop deriving `phase` from the clause language; use a predicate over the raised
   recognizer, defaulting to the old answer when unknown.
4. **#115 (arbitrated):** in `assign_placeholders`, a value claimed by more than one prefix gets ONE
   placeholder, from the most specific type (a `_PREFIX_PRIORITY` table: structured types beat
   `PARTY`/`NAME`/`ADDRESS`/`DATE`). Do NOT touch `detect_on_page`'s entity count or
   `benchmark/metrics_pii.py`. The stray-placeholder append loop stays (metadata only).

GREEN: `uv run pytest -q tests/unit/test_pii_placeholders.py tests/unit/test_pii_engine.py
tests/unit/test_pii_fail_closed.py tests/unit/test_pii_recognizers.py
tests/integration/test_pii_accuracy.py tests/integration/test_pii_strip_command.py
tests/integration/test_benchmark_pii_accuracy.py tests/unit/test_benchmark_metrics_pii.py`.

## Task E — clause hierarchy (#108)

Files: `src/openreview_cli/parsing/clause_detector.py`, `src/openreview_cli/parsing/pdf_parser.py`,
`src/openreview_cli/parsing/docx_parser.py`, `src/openreview_cli/tui/screens/graph.py`,
`tests/integration/test_stream_clauses.py`.

1. Correct `_NUMBERING_PATTERNS` (design §3.9 item 1) — the dotted forms must be tried before the bare
   `Section <num>` form; add `_extract_numbering_level(line)`.
2. Add `link_parent_ids(clauses, open_levels, *, levels=None)` in `clause_detector.py` and wire it into
   `pdf_parser.py` (one stack across the page loop) and `docx_parser.py` (heading level wins over
   numbering). `build_hierarchy`'s signature/behaviour is unchanged.
3. Reword the graph caveat's parenthetical in `tui/screens/graph.py` (keep the asserted first sentence
   verbatim); fix the stale line reference in `tests/redteam/test_redaction_ordering.py`'s docstring.
4. Strengthen `tests/integration/test_stream_clauses.py::test_parent_id_chain_integrity` (assert
   `any(c.parent_id ...)` for the numbered fixture, none for the flat one). `nda_with_pii.pdf` pins must
   NOT change (it has no numbering).

GREEN: `uv run pytest -q tests/unit/test_clause_detector.py tests/integration/test_stream_clauses.py
tests/unit/test_tui_graph_domain.py tests/integration/tui/test_graph_screen.py
tests/unit/test_graph_builder.py tests/unit/test_graph_metrics.py tests/unit/test_chunking_splitter.py
tests/integration/test_chunking_cli.py`.

## Lead QC (after all five)

`uv run ruff check . && uv run ruff format .` · `uv run mypy src/ tests/` ·
`uv run pytest -m fast -q` · then the six fuzz/redteam/chaos files named above.
