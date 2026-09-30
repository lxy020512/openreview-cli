# Windows baseline environment

Verified on 2026-09-30. This report records installation and offline checks; it does not claim that live model review has passed.

## Source and Git

- Upstream checkout: `E:\Projects\openreview-cli`.
- Development worktree: `E:\Projects\openreview-cli-resume`.
- Fixed baseline: `db184390e7b23e052c68ef3b04022dc5befec9c1`.
- Development branch: `codex/clause-checkpoints`.
- `origin`: `https://github.com/lxy020512/openreview-cli.git`.
- `upstream`: `https://github.com/mohamed-benoughidene/openreview-cli.git`.
- No changes were pushed during environment setup.

E: uses exFAT, which does not record Git ownership and cannot share package files with the C: cache using NTFS hardlinks. Git trusts only the two exact checkout paths through `safe.directory`; no wildcard trust was configured.

## Runtime

- Portable uv: `E:\Projects\.tools\uv\uv.exe`, version 0.12.21.
- Official archive SHA256: `5d223efa0bf00208c3853246af09420419dfbd352536aa6bb8163d6170e23890`; matched the official release checksum.
- Base Python: `C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`, version 3.12.14.
- Dedicated virtual environment: `C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime`.
- The pre-existing workspace `.venv` was not modified.
- 122 packages were installed from the project lockfile, including CPU-only `torch==2.13.0+cpu`.
- The dedicated environment is now editable-bound to the development worktree. Do not sync it back to the upstream checkout while development or tests are running.

Source stays on E:. The reproducible environment lives on C: because copying package files onto exFAT was slow. An interrupted, incomplete `E:\Projects\openreview-cli\.venv` was retained rather than deleted. It must not be used as the runtime.

## Start from PowerShell

```powershell
Set-Location 'E:\Projects\openreview-cli-resume'
$env:UV_PROJECT_ENVIRONMENT = 'C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime'
$env:PYTHONUTF8 = '1'
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview --help
```

`PYTHONUTF8=1` is required on this Windows machine: upstream SQL migration files contain UTF-8 characters, while the system default encoding is GBK. Without the setting, CLI initialization failed with `UnicodeDecodeError`. No upstream product code was changed to work around it.

To resync locked dependencies explicitly:

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime'
$env:PYTHONUTF8 = '1'
& 'E:\Projects\.tools\uv\uv.exe' sync --locked --python 'C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' --no-python-downloads
```

These are process settings; neither the global PATH nor existing Python environments were changed.

## Verified upstream baseline

The checks ran on the fixed upstream commit before the environment was rebound to the worktree.

- `openreview --help`: exit 0.
- `openreview parse tests/fixtures/nda_with_pii.pdf --summary` with UTF-8 mode: exit 0; 5 clauses on 1 page.
- Direct parsing of the same public fixture: 5 clauses on 1 page; cold parse duration 1.2419 seconds. This measures parsing only, not review accuracy or end-to-end latency.
- 116 existing tests passed in 5.56 seconds, across `test_review_pipeline`, `test_extraction_agent`, `test_qa_agent`, `test_recovery_persistence`, `test_recovery_json_safe`, `test_pipeline_runner`, `test_pdf_parser`, `test_docx_parser`, `test_parsing_models`, and `test_review_models`.
- Model downloads were disabled using `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
- Tests excluded memory, network and live markers. Sockets remained restricted to Unix sockets and loopback hosts `127.0.0.1,::1`, which Windows asyncio needs for its socket pair.
- The first test attempt hit a denied shared system pytest temporary directory. A dedicated fresh `--basetemp` under the runtime environment resolved that environmental issue. No pytest project settings were changed.

Logs and JUnit output are in the upstream checkout's ignored `review_results\baseline` directory. CLI state for the public-fixture smoke test was also isolated there; it contains no real model credentials.

For later tests, use a new temporary path so pytest cannot clean unrelated files:

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$testRoot = Join-Path $env:UV_PROJECT_ENVIRONMENT 'test-tmp'
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$testTemp = Join-Path $testRoot ([guid]::NewGuid().ToString('N'))
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync pytest tests/unit/test_review_pipeline.py tests/unit/test_recovery_persistence.py -q -o 'addopts=--tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1' --basetemp $testTemp
```

## Remaining prerequisites

The list below records the initial setup state, not the current feature status. The NLP model has since been installed and verified in [pii-baseline.md](pii-baseline.md); authorized local DeepSeek configuration and authenticated model-catalog access succeeded. Initial live review exposed a strict prompt/schema mismatch; after the narrow template repair, the public-NDA smoke and same-process cache repeat passed, as recorded in [deepseek-setup.md](deepseek-setup.md). Independent pre-commit tooling was prepared without adding a project dependency; earlier checks passed in both the C: draft and E: worktree, and each commit records its own hook results. The initial failures and measurements are retained for comparison.

- No Ollama or local LLM was installed. The selected provider is a cloud API, to be configured locally later.
- The spaCy package is installed, but the `en_core_web_lg` NLP model is not. Repository notes estimate the model at roughly 600 MB. Real local PII detection has not passed yet; cloud review must retain the privacy gate rather than bypass it.
- No live review, model cost measurement, review-accuracy benchmark, memory suite or full repository test suite was run.
- `pre-commit` was not available in PATH and its hook was not installed. Required hook checks remain a separate prerequisite before a commit.
- Store real API keys only in the project's supported local authentication location or the process environment. Do not put them in this document, the repository, command arguments, or test logs. On Windows, use the default user configuration directory on NTFS rather than storing credentials on the exFAT source drive.

## Later Windows test prerequisite

Latest verification record updated on 2026-10-01; the original 2026-09-30 setup measurements above remain unchanged.

The unchanged upstream test modules `test_output_write_errors.py` and `test_chaos_resource_limits.py` initially failed collection and strict mypy on Windows because their module-level skip conditions called POSIX-only `os.geteuid()`. A narrowly scoped guard uses `getattr` and skips only directory-permission tests that require POSIX chmod semantics, retaining the original non-root Linux assertions. This changes test portability, not product behavior or tool configuration.

Independent review checked eight AST conditions; the two modules collected all 19 tests, and full `mypy src/ tests/` passed for 667 files. Runtime checks produced 9 passed, 8 skipped, 1 memory test deselected, and 1 unchanged failure: a Linux `Is a directory` string assertion differs from Windows' permission error. Linux runtime and Windows ACL behavior were not exercised. Full evidence and scope are in [windows-test-task.md](../specs/036-clause-checkpoints/windows-test-task.md).

The required `pre-commit run --all-files` subsequently passed all 10 hooks in the C: draft, including ruff, mypy, and the pytest collection hook. The synchronized E: worktree initially passed 47 new tests in 11.50 seconds, applicable hooks, and mypy for 667 files. Following the live-smoke prompt repair, 78 new tests in 12 files passed on E: in 8.33 seconds; independent review verified strict-template and compatibility cases. The pytest hook uses `--collect-only`, so it does not execute the full unit suite. Each commit records its own hook results. The real smoke, same-process repeat, and guarded fresh-process disk-cache restore passed; accuracy, billed amounts, general latency improvements, and production reliability remain unverified.
