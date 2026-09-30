from pathlib import Path

import pytest

"""Persisted prompt binding invalidates a run; unrelated rows do not."""


def test_prompt_binding_and_unrelated_database_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openreview_cli.gateway.models import ProviderInfo
    from openreview_cli.prompts.store import PromptStore
    from openreview_cli.review.checkpoints import runtime_snapshot
    from openreview_cli.storage.database import get_connection, init_database

    config = {
        "gateway": {
            "models": {"extraction": {"primary": "ollama/m"}, "reasoning": {"primary": "ollama/m"}}
        }
    }
    providers = {
        "ollama": ProviderInfo(name="ollama", base_url="http://localhost:11434", is_local=True)
    }
    monkeypatch.setattr("openreview_cli.config.loader.load_config", lambda _: config)
    monkeypatch.setattr("openreview_cli.config.auth.load_auth", lambda _: {})
    monkeypatch.setattr("openreview_cli.gateway.registry.load_registry", lambda: providers)
    monkeypatch.setattr("openreview_cli.gateway.router.load_registry", lambda: providers)
    db = tmp_path / "db.sqlite"
    init_database(db)
    store = PromptStore(db)
    store.create("extract", "first prompt")
    store.bind("extraction", "extract", 1)
    first = runtime_snapshot(("extraction", "reasoning"), db)
    with get_connection(db) as conn:
        conn.execute("CREATE TABLE unrelated_runtime (value TEXT)")
        conn.execute("INSERT INTO unrelated_runtime VALUES ('updated')")
        conn.commit()
    assert runtime_snapshot(("extraction", "reasoning"), db) == first
    store.update("extract", "second prompt")
    store.bind("extraction", "extract", 2)
    assert runtime_snapshot(("extraction", "reasoning"), db) != first
