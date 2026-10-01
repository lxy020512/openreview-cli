"""Offline contracts for current DeepSeek registry and request routing."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openreview_cli.gateway.errors import CapabilityMismatchError, PIIUnavailableError
from openreview_cli.gateway.models import CapabilityRequirement
from openreview_cli.gateway.registry import load_registry
from openreview_cli.gateway.router import Gateway, mark_pii_available, reset_pii_available


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openreview_cli.gateway import registry

    monkeypatch.setattr(registry, "_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_current_models_have_review_slots_and_million_token_context(
    model: str, isolated_registry: None
) -> None:
    provider = load_registry()["deepseek"]
    entry = provider.models[model]
    assert {"extraction", "reasoning", "grounding"} <= set(entry.slots)
    assert entry.status == "active"
    assert entry.context == 1_000_000
    assert provider.capabilities.context_window == 1_000_000
    assert provider.capabilities.reasoning is True
    assert provider.capabilities.tool_call is True
    assert provider.capabilities.embedding is False
    assert provider.capabilities.rerank is False


def test_legacy_model_remains_parseable_but_is_not_recommended(isolated_registry: None) -> None:
    entry = load_registry()["deepseek"].models["deepseek-chat"]
    assert entry.status == "deprecated"
    assert entry.recommended is False
    assert entry.note and "retired" in entry.note.lower()


def _gateway(tmp_path: Path, model: str, *, json_mode: bool = True) -> Gateway:
    import json

    import yaml

    from openreview_cli.storage.database import init_database

    slot: dict[str, Any] = {"primary": f"deepseek/{model}"}
    if json_mode:
        slot["extra_params"] = {"response_format": {"type": "json_object"}}
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {"privacy": {"tier": "balanced"}, "gateway": {"models": {"extraction": slot}}}
        ),
        encoding="utf-8",
    )
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"deepseek": "offline-placeholder"}), encoding="utf-8")
    data = tmp_path / "review.db"
    init_database(data)
    return Gateway(config, auth, data)


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_real_gateway_builds_native_deepseek_json_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_registry: None, model: str
) -> None:
    from openreview_cli.gateway import router

    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
        )

    monkeypatch.setattr(router, "completion", fake_completion)
    gateway = _gateway(tmp_path, model)
    monkeypatch.setattr(gateway._cost_tracker, "log_call", lambda *args: None)
    mark_pii_available()
    try:
        result = gateway.chat(
            "extraction",
            [{"role": "user", "content": 'Return JSON matching {"ok": true}.'}],
            requirement=CapabilityRequirement(capability="reasoning", min_context_window=100_000),
        )
        assert result == '{"ok":true}'
        assert len(calls) == 1
        assert calls[0]["model"] == f"deepseek/{model}"
        assert calls[0]["api_base"] == "https://api.deepseek.com"
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert os.environ["DEEPSEEK_API_KEY"] == "offline-placeholder"
        with pytest.raises(CapabilityMismatchError):
            gateway.validate_capability(
                load_registry()["deepseek"], CapabilityRequirement(capability="embedding")
            )
    finally:
        gateway.clear_env_vars()
        reset_pii_available()


def test_balanced_privacy_blocks_deepseek_before_pii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_registry: None
) -> None:
    from openreview_cli.gateway import router

    gateway = _gateway(tmp_path, "deepseek-flash")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(router, "completion", lambda **kwargs: calls.append(kwargs))
    reset_pii_available()
    try:
        with pytest.raises(PIIUnavailableError):
            gateway.chat("extraction", [{"role": "user", "content": "Return JSON."}])
        assert calls == []
    finally:
        gateway.clear_env_vars()


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_installed_litellm_resolves_current_alias_and_json_payload(model: str) -> None:
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
    from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig

    bare_model, provider, _, base = get_llm_provider(
        model=f"deepseek/{model}",
        api_base="https://api.deepseek.com",
        api_key="offline-placeholder",
    )
    assert (bare_model, provider, base) == (model, "deepseek", "https://api.deepseek.com")
    adapter = DeepSeekChatConfig()
    assert (
        adapter.get_complete_url(
            api_base=base, api_key=None, model=bare_model, optional_params={}, litellm_params={}
        )
        == "https://api.deepseek.com/chat/completions"
    )
    payload = adapter.transform_request(
        model=bare_model,
        messages=[{"role": "user", "content": 'Return JSON matching {"ok": true}.'}],
        optional_params={"response_format": {"type": "json_object"}},
        litellm_params={},
        headers={},
    )
    assert payload["model"] == bare_model
    assert payload["response_format"] == {"type": "json_object"}
