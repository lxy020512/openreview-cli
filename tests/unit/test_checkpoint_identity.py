from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from openreview_cli.review.checkpoints import build_identity, runtime_snapshot


def test_identity_binds_complete_inputs_without_leaking_credentials(tmp_path: Path) -> None:
    doc = tmp_path / "nda.docx"
    doc.write_bytes(b"nda-v1")
    key = Fernet.generate_key()
    base: dict[str, Any] = {
        "settings": {"playbook": {"description": "v1"}},
        "runtime": {"secret": "API-SECRET"},
        "manifest": {"parser": "v1"},
        "key": key,
    }
    first = build_identity(doc, **base)
    assert "API-SECRET" not in repr(first)
    for field, value in [
        ("settings", {"playbook": {"description": "v2"}}),
        ("runtime", {"secret": "new"}),
        ("manifest", {"parser": "v2"}),
    ]:
        updated = {**base, field: value}
        assert build_identity(doc, **updated) != first
    second_path = tmp_path / "copy.docx"
    second_path.write_bytes(doc.read_bytes())
    assert build_identity(second_path, **base) != first
    doc.write_bytes(b"nda-v2")
    assert build_identity(doc, **base) != first


def test_runtime_binds_resolved_prompts_fallback_credentials_and_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openreview_cli.gateway.models import CredentialField, ProviderInfo
    from openreview_cli.prompts.store import PromptStore

    config = {
        "gateway": {
            "models": {
                "extraction": {"primary": "p/m", "fallback": "q/n"},
                "reasoning": {"primary": "p/m"},
            }
        }
    }
    registry = {
        name: ProviderInfo(
            name=name,
            base_url="https://" + name + ".example",
            credentials=[
                CredentialField(env_key=name.upper() + "_KEY", label="key", litellm_param="api_key")
            ],
        )
        for name in ("p", "q")
    }
    monkeypatch.setattr("openreview_cli.config.loader.load_config", lambda _: config)
    monkeypatch.setattr("openreview_cli.config.auth.load_auth", lambda _: {})
    monkeypatch.setattr("openreview_cli.gateway.registry.load_registry", lambda: registry)
    monkeypatch.setattr("openreview_cli.gateway.router.load_registry", lambda: registry)
    prompt = {"value": "resolved system prompt"}
    monkeypatch.setattr(PromptStore, "resolve", lambda self, slot: prompt["value"])
    db = tmp_path / "db.sqlite"
    first = runtime_snapshot(("extraction", "reasoning"), db)
    prompt["value"] = "new resolved prompt"
    assert runtime_snapshot(("extraction", "reasoning"), db) != first
    prompt["value"] = "resolved system prompt"
    monkeypatch.setenv("Q_KEY", "FALLBACK-SECRET")
    assert runtime_snapshot(("extraction", "reasoning"), db) != first
    monkeypatch.delenv("Q_KEY")
    registry["q"].base_url = "https://new.example"
    assert runtime_snapshot(("extraction", "reasoning"), db) != first


def test_real_gateway_environment_seeding_keeps_effective_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from openreview_cli.gateway import router
    from openreview_cli.gateway.cost import CostTracker
    from openreview_cli.gateway.models import ProviderInfo
    from openreview_cli.prompts.store import PromptStore

    config = {
        "gateway": {
            "models": {
                "extraction": {"primary": "openai/model"},
                "reasoning": {"primary": "openai/model"},
            }
        }
    }
    auth = {"openai": "SYNTHETIC-AUTH-TOKEN"}
    registry = {
        "openai": ProviderInfo(
            name="openai", env_key="OPENAI_API_KEY", base_url="https://api.openai.com"
        )
    }
    monkeypatch.setattr("openreview_cli.config.loader.load_config", lambda _: config)
    monkeypatch.setattr("openreview_cli.config.auth.load_auth", lambda _: auth)
    monkeypatch.setattr("openreview_cli.gateway.registry.load_registry", lambda: registry)
    monkeypatch.setattr(router, "load_config", lambda _: config)
    monkeypatch.setattr(router, "load_auth", lambda _: auth)
    monkeypatch.setattr(router, "load_registry", lambda: registry)
    monkeypatch.setattr(PromptStore, "resolve", lambda self, slot: None)
    monkeypatch.setattr(router.Gateway, "_enforce_tier", lambda *a, **k: None)
    monkeypatch.setattr(router.Gateway, "_check_cost_limits", lambda *a, **k: None)
    monkeypatch.setattr(CostTracker, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(
        router,
        "completion",
        lambda **k: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
        ),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = runtime_snapshot(("extraction", "reasoning"), tmp_path / "openreview.db")
    gw = router.Gateway(data_path=tmp_path / "openreview.db")
    try:
        assert gw.chat("extraction", [{"role": "user", "content": "synthetic"}]) == "{}"
        assert runtime_snapshot(("extraction", "reasoning"), tmp_path / "openreview.db") == first
    finally:
        gw.clear_env_vars()
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC-OVERRIDE")
    assert runtime_snapshot(("extraction", "reasoning"), tmp_path / "openreview.db") != first


def test_fixed_manifest_binds_shared_json_parser_and_slot_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openreview_cli.review import checkpoints

    package = tmp_path / "synthetic-package"
    (package / "review").mkdir(parents=True)
    monkeypatch.setattr(checkpoints, "__file__", str(package / "review" / "checkpoints.py"))
    for name in ("llm_json.py", "slots.py"):
        (package / name).write_text("version one", encoding="utf-8")
    first = checkpoints.implementation_manifest()
    document = tmp_path / "nda.docx"
    document.write_bytes(b"synthetic")
    key = Fernet.generate_key()
    first_identity = build_identity(document, settings={}, runtime={}, manifest=first, key=key)
    (package / "llm_json.py").write_text("version two", encoding="utf-8")
    second = checkpoints.implementation_manifest()
    assert second != first
    assert (
        build_identity(document, settings={}, runtime={}, manifest=second, key=key)
        != first_identity
    )
    (package / "slots.py").write_text("version two", encoding="utf-8")
    assert checkpoints.implementation_manifest() != second
