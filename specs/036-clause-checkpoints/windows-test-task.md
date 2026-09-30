# Windows test collection prerequisite

Status: approved routine prerequisite for the authorized Windows reproduction.

## Problem and evidence

On Windows with Python 3.12.14, collecting
`tests/unit/test_output_write_errors.py` and
`tests/chaos/test_chaos_resource_limits.py` failed before any test could run:
the module-level skip conditions called the POSIX-only `os.geteuid()`.
The existing strict mypy check also reported `attr-defined` in those two files.
This prevented the repository's pre-commit unit-test collection hook from
validating the checkpoint changes.

## Decision and scope

Skip only the existing tests that use `chmod` to remove directory write
permissions on Windows and for root users. Windows `chmod` does not enforce the
POSIX directory permissions these tests require. Resolve `geteuid` with
`getattr`, keeping the non-root POSIX behavior and all original assertions.

The task changes those two test modules and this record only. Product behavior,
test selection configuration, hooks, and unrelated Windows assertions stay
outside this task. This does not add validation of Windows access-control lists.

## Validation

The failure was reproduced with focused test collection and focused strict mypy
before editing. After the fix:

- Both affected modules collected all 19 tests successfully.
- `uv run --no-sync mypy src/ tests/` passed for 667 source files.
- Ruff lint and format checks passed for both changed test modules.
- The affected modules' runtime run with `-m "not memory"` produced 9 passed,
  8 skipped, 1 deselected, and 1 failed. The remaining unchanged failure,
  `test_write_output_file_target_is_directory_clean_error`, expects Linux's
  `Is a directory` diagnostic; Windows reports a permission error for writing
  to a directory. It remains visible and is outside this collection fix.

No memory test was included in the combined runtime run. Linux runtime checks
and Windows ACL enforcement were not exercised by this task.
