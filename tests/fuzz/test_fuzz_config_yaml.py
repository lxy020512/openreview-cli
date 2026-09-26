"""W4 fuzz suite: the ``config.yml`` ingestion boundary.

Sharp edges 5 and 6 (plan section 3). The three unguarded ``yaml.safe_load``
sites are ``config/loader.py:288`` (``set_config_value``), ``:318`` (``load_config``)
and ``:340`` (``add_custom_provider``); the pydantic raise is ``:227``
(``_validate_and_merge``). The CLI reaches ``:318`` first, through the root
callback ``_init`` (``app.py:257``), before any subcommand runs.

Expected behaviour asserted here: every malformed ``config.yml`` maps to exit 5
(``EXIT_CONFIG``) with the documented ``Config error`` token and no raw
traceback. Observed today the CLI exits 1 with the raw exception. The library
sites ``:288`` and ``:340`` are unreachable through the CLI with a corrupt config
(``_init`` fails first), so they are driven directly.

Known-broken behaviour carries ``xfail(strict=True)`` tied to a register row.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

import openreview_cli.app as app_module
from openreview_cli.config import loader
from openreview_cli.errors import EXIT_CONFIG
from tests.fuzz import _state_probe
from tests.helpers import corpus_state

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.fuzz

_TIMEOUT_SECONDS = 20.0


# ── CLI: a malformed config.yml must be a clean exit-5 config error ─────────


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            corpus_state.corrupt_yaml,
            id="corrupt-yaml",
            marks=pytest.mark.xfail(
                strict=True,
                reason="RT-020: yaml.YAMLError escapes load_config (loader.py:318) uncaught; "
                "the CLI exits 1 with a raw ParserError instead of exit 5",
            ),
        ),
        pytest.param(
            corpus_state.list_not_map_yaml,
            id="list-not-map",
            marks=pytest.mark.xfail(
                strict=True,
                reason="RT-020: a top-level list config makes _deep_merge (loader.py:231) raise "
                "AttributeError; the CLI exits 1 instead of exit 5",
            ),
        ),
        pytest.param(
            corpus_state.wrong_typed_yaml,
            id="wrong-typed",
            marks=pytest.mark.xfail(
                strict=True,
                reason="RT-021: a wrongly typed config raises raw pydantic ValidationError from "
                "_validate_and_merge (loader.py:227); the CLI exits 1 instead of exit 5",
            ),
        ),
    ],
)
def test_config_malformed_cli_is_a_clean_config_error(
    build: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    build(state.config_dir)
    with _state_probe.count_calls(app_module, "load_config") as hits:
        result = _state_probe.run_cli(["config", "get", "privacy.tier"])
    assert hits[0] >= 1, "the config loader was never reached"
    _state_probe.assert_clean_failure(result, frozenset({EXIT_CONFIG}), "Config error")


# ── CLI: extreme but well-formed config must be handled, not crash or hang ───


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(corpus_state.deeply_nested_yaml, id="deeply-nested"),
        pytest.param(corpus_state.billion_laughs_yaml, id="billion-laughs"),
    ],
)
def test_config_extreme_well_formed_cli_is_handled(
    build: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    build(state.config_dir)
    started = time.perf_counter()
    with _state_probe.count_calls(app_module, "load_config") as hits:
        result = _state_probe.run_cli(["config", "get", "privacy.tier"])
    elapsed = time.perf_counter() - started
    assert hits[0] >= 1, "the config loader was never reached"
    assert elapsed < _TIMEOUT_SECONDS, f"config case hung: {elapsed:.1f}s"
    assert result.exit_code == 0, f"{result.exit_code}: {result.output!r}"
    assert "balanced" in result.output
    assert _state_probe.TRACEBACK_MARKER not in result.output


# ── Library: the three unguarded yaml.safe_load sites and the pydantic raise ─


def test_library_load_config_reaches_the_yaml_parse(tmp_path: Path) -> None:
    """The ``loader.py:318`` site is reached and the YAMLError is raw."""
    path = corpus_state.corrupt_yaml(tmp_path)
    with (
        _state_probe.count_calls(loader, "load_config") as hits,
        pytest.raises(yaml.YAMLError),
    ):
        loader.load_config(path)
    assert hits[0] >= 1


def test_library_set_config_value_reaches_the_yaml_parse(tmp_path: Path) -> None:
    """The ``loader.py:288`` site is reached and the YAMLError is raw."""
    path = corpus_state.corrupt_yaml(tmp_path)
    with (
        _state_probe.count_calls(loader, "set_config_value") as hits,
        pytest.raises(yaml.YAMLError),
    ):
        loader.set_config_value(path, "privacy.tier", "maximum")
    assert hits[0] >= 1


def test_library_add_custom_provider_reaches_the_yaml_parse(tmp_path: Path) -> None:
    """The ``loader.py:340`` site is reached and the YAMLError is raw."""
    path = corpus_state.corrupt_yaml(tmp_path)
    with (
        _state_probe.count_calls(loader, "add_custom_provider") as hits,
        pytest.raises(yaml.YAMLError),
    ):
        loader.add_custom_provider(path, "p", "http://x", "P_API_KEY")
    assert hits[0] >= 1


def test_library_load_config_reaches_the_pydantic_validation(tmp_path: Path) -> None:
    """The ``loader.py:227`` pydantic raise is reachable and raw."""
    path = corpus_state.wrong_typed_yaml(tmp_path)
    with (
        _state_probe.count_calls(loader, "_validate_and_merge") as hits,
        pytest.raises(ValidationError),
    ):
        loader.load_config(path)
    assert hits[0] >= 1


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_config_oracle_rejects_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid config is NOT a config error; the oracle must reject it.

    ``assert_clean_failure`` is the predicate every malformed-input case trusts.
    Feeding it a successful run (exit 0) must make it raise, or a green suite
    would certify nothing (plan section 9.3).
    """
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    assert state.config_path.exists()
    with _state_probe.count_calls(app_module, "load_config") as hits:
        result = _state_probe.run_cli(["config", "get", "privacy.tier"])
    assert hits[0] >= 1
    assert result.exit_code == 0, f"{result.exit_code}: {result.output!r}"
    with pytest.raises(AssertionError):
        _state_probe.assert_clean_failure(result, frozenset({EXIT_CONFIG}), "Config error")
