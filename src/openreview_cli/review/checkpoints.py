"""Opt-in review checkpoints: private payloads and conservative run identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from openreview_cli.pipeline.errors import CheckpointError
from openreview_cli.review.models import ClauseAssessment, Position, QAVerdict

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
__all__ = [
    "CheckpointCodec",
    "CheckpointError",
    "CheckpointSession",
    "build_identity",
    "document_hash",
    "implementation_manifest",
    "load_checkpoint_key",
    "runtime_snapshot",
]


class CheckpointCodec:
    """Explicit authenticated assessment schema; source text is never persisted."""

    def __init__(self, key: bytes) -> None:
        try:
            self.fernet = Fernet(key)
        except (ValueError, TypeError):
            raise CheckpointError("checkpoint key unavailable") from None

    def encode(self, assessment: ClauseAssessment) -> bytes:
        if assessment.error is not None:
            raise CheckpointError("checkpoint result invalid")
        data = {
            "schema_version": SCHEMA_VERSION,
            "clause_id": assessment.clause_id,
            "category": assessment.playbook_category,
            "position": assessment.position.value,
            "confidence": assessment.confidence,
            "citation": assessment.citation,
            "extraction_model": assessment.extraction_model,
            "qa_model": assessment.qa_model,
            "qa_verdict": assessment.qa_verdict.value,
            "qa_revised_position": (
                assessment.qa_revised_position.value if assessment.qa_revised_position else None
            ),
            "qa_revised_rationale": assessment.qa_revised_rationale,
            "is_amber": assessment.is_amber,
        }
        try:
            raw = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise CheckpointError("checkpoint result invalid") from None
        # Apply the same validator to writes and reads.
        blob = self.fernet.encrypt(raw)
        if (
            self.decode(
                blob,
                clause_id=assessment.clause_id,
                category=assessment.playbook_category,
                text="",
                extraction_model=assessment.extraction_model,
                qa_model=assessment.qa_model,
            )
            is None
        ):
            raise CheckpointError("checkpoint result invalid")
        return blob

    def decode(
        self,
        blob: bytes,
        *,
        clause_id: str,
        category: str,
        text: str,
        extraction_model: str,
        qa_model: str,
    ) -> ClauseAssessment | None:
        try:
            data = json.loads(self.fernet.decrypt(blob))
            expected = {
                "schema_version",
                "clause_id",
                "category",
                "position",
                "confidence",
                "citation",
                "extraction_model",
                "qa_model",
                "qa_verdict",
                "qa_revised_position",
                "qa_revised_rationale",
                "is_amber",
            }
            if (
                not isinstance(data, dict)
                or set(data) != expected
                or type(data["schema_version"]) is not int
                or data["schema_version"] != SCHEMA_VERSION
            ):
                return None
            if not all(
                isinstance(data[field], str)
                for field in (
                    "clause_id",
                    "category",
                    "position",
                    "citation",
                    "extraction_model",
                    "qa_model",
                    "qa_verdict",
                )
            ):
                return None
            if (
                data["clause_id"],
                data["category"],
                data["extraction_model"],
                data["qa_model"],
            ) != (clause_id, category, extraction_model, qa_model):
                return None
            confidence = data["confidence"]
            rationale = data["qa_revised_rationale"]
            revised = data["qa_revised_position"]
            if (
                type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
                or type(data["is_amber"]) is not bool
                or (rationale is not None and not isinstance(rationale, str))
                or (revised is not None and not isinstance(revised, str))
            ):
                return None
            assessment = ClauseAssessment(
                clause_id,
                text,
                category,
                Position(data["position"]),
                confidence,
                data["citation"],
                QAVerdict(data["qa_verdict"]),
                extraction_model,
                qa_model,
                qa_revised_position=Position(revised) if revised is not None else None,
                qa_revised_rationale=rationale,
            )
            assessment.is_amber = data["is_amber"]
        except (InvalidToken, ValueError, TypeError, KeyError, OverflowError):
            return None
        else:
            return assessment


def load_checkpoint_key(path: Path) -> bytes:
    """Create once in the user config directory; never replace an invalid key."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        key = path.read_bytes()
        Fernet(key)
    except (OSError, ValueError, TypeError):
        raise CheckpointError("checkpoint key unavailable") from None
    else:
        return key


def document_hash(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise CheckpointError("checkpoint document unavailable") from None


def _digest(data: Any, key: bytes) -> str:
    try:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hmac.new(key, raw, hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        raise CheckpointError("checkpoint configuration unavailable") from None


def build_identity(
    path: Path,
    *,
    settings: dict[str, Any],
    runtime: dict[str, Any],
    manifest: dict[str, Any],
    key: bytes,
) -> tuple[str, str]:
    file_hash = document_hash(path)
    path_hash = hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "document": file_hash,
            "path": path_hash,
            "settings": settings,
            "runtime": runtime,
            "manifest": manifest,
        },
        key,
    ), file_hash


def implementation_manifest() -> dict[str, Any]:
    """Fixed review packages/dependencies; captured once per invocation."""
    from importlib.metadata import PackageNotFoundError, version

    root = Path(__file__).resolve().parents[1]
    files = {}
    for package in (
        "parsing",
        "pii",
        "review",
        "gateway",
        "config",
        "prompts",
        "pipeline/adapters",
    ):
        for source in sorted((root / package).rglob("*.py")):
            files[source.relative_to(root).as_posix()] = hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
    # Shared request/response helpers and fallback policy also affect results.
    for relative in ("llm_json.py", "slots.py", "pipeline/errors.py", "pipeline/runner.py"):
        source = root / relative
        if source.is_file():
            files[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    dependencies: dict[str, str | None] = {}
    for package in (
        "PyMuPDF",
        "python-docx",
        "presidio-analyzer",
        "presidio-anonymizer",
        "spacy",
        "en-core-web-lg",
        "litellm",
        "cryptography",
        "nupunkt",
    ):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    return {"files": files, "dependencies": dependencies}


def _effective_auth_environment(auth: dict[str, Any]) -> dict[str, str]:
    from openreview_cli.config.auth import key_to_env

    effective_env = dict(os.environ)
    for provider, stored_credentials in auth.items():
        if isinstance(stored_credentials, str):
            effective_env.setdefault(key_to_env(provider), stored_credentials)
        elif isinstance(stored_credentials, dict):
            for name, value in stored_credentials.items():
                if value:
                    effective_env.setdefault(name, value)
    return effective_env


def _candidate_details(
    config: dict[str, Any], auth: dict[str, Any], candidate: str, slot: str, info: Any
) -> dict[str, Any]:
    from copy import deepcopy

    from openreview_cli.gateway.router import Gateway

    gw = Gateway.__new__(Gateway)
    gw._config = deepcopy(config)
    gw._auth = auth
    gw._config["gateway"]["models"][slot]["primary"] = candidate
    kwargs = gw._get_litellm_kwargs(slot)
    file_credentials = {}
    for field in info.credentials:
        value = kwargs.get(field.litellm_param)
        if field.is_file_path and isinstance(value, str):
            try:
                file_credentials[field.litellm_param] = Path(value).read_bytes().hex()
            except OSError:
                raise CheckpointError("checkpoint credential unavailable") from None
    return {
        "provider": info.model_dump(mode="json"),
        "kwargs": kwargs,
        "credential_files": file_credentials,
    }


def runtime_snapshot(slots: tuple[str, str], db_path: Path) -> dict[str, Any]:
    """Resolve config, auth, prompts and every candidate's effective credentials.

    Sensitive values remain in memory and are immediately HMACed by the caller.
    An uninitialized Gateway reuses the exact kwargs resolver without seeding
    environment variables, creating cost tracking, or making network requests.
    """
    from openreview_cli.config.auth import key_to_env, load_auth
    from openreview_cli.config.loader import load_config
    from openreview_cli.config.paths import get_config_dir
    from openreview_cli.gateway.registry import load_registry
    from openreview_cli.prompts.store import PromptStore
    from openreview_cli.prompts.variables import substitute

    config_dir = get_config_dir()
    config = load_config(config_dir / "config.yml")
    auth = load_auth(config_dir / "auth.json")
    # Gateway seeds absent variables from auth.json on construction. Fingerprint
    # their effective values, so this internal seeding is not mistaken for drift.
    effective_env = _effective_auth_environment(auth)
    registry = load_registry()
    models = config.get("gateway", {}).get("models", {})
    candidates: list[str] = []
    for slot in slots:
        cfg = models.get(slot, {})
        for name in ("primary", "fallback"):
            value = cfg.get(name)
            if isinstance(value, str) and value not in candidates:
                candidates.append(value)
    effective = {}
    env_names = {
        "OLLAMA_HOST",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "AZURE_API_BASE",
        "AZURE_API_VERSION",
        "LITELLM_API_BASE",
    }
    for candidate in candidates:
        prefix = candidate.split("/", 1)[0]
        info = registry.get(prefix)
        if info is None:
            raise CheckpointError("checkpoint provider unavailable")
        env_name = key_to_env(prefix)
        if env_name:
            env_names.add(env_name)
        if info.env_key:
            env_names.add(info.env_key)
        env_names.update(field.env_key for field in info.credentials)
        stored = auth.get(prefix)
        if isinstance(stored, dict):
            env_names.update(stored)
        for slot in slots:
            if not models.get(slot, {}).get("primary"):
                raise CheckpointError("checkpoint provider unavailable")
            effective[f"{slot}:{candidate}"] = _candidate_details(
                config, auth, candidate, slot, info
            )
    prompts = {}
    store = PromptStore(db_path)
    for slot in slots:
        resolved = store.resolve(slot)
        prompts[slot] = substitute(resolved, slot, {}) if resolved else None
    return {
        "config": config,
        "auth": auth,
        "providers": effective,
        "prompts": prompts,
        "environment": {name: effective_env.get(name) for name in sorted(env_names)},
    }


class CheckpointSession:
    """One same-file run, with small extraction/QA operations and a drift guard."""

    def __init__(
        self,
        path: Path,
        *,
        settings: Callable[[], dict[str, Any]],
        slots: tuple[str, str],
        db_path: Path,
        key_path: Path,
        session_id: str | None = None,
        force: bool = False,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        from openreview_cli.storage.checkpoints import CheckpointStore

        self.path = path
        self.settings = settings
        self.slots = slots
        self.db_path = db_path
        self.key = load_checkpoint_key(key_path)
        self.codec = CheckpointCodec(self.key)
        self.store = CheckpointStore(db_path)
        self.manifest = implementation_manifest() if manifest is None else manifest
        self.identity, file_hash = self._identity()
        self.run = self.store.get_or_create(self.identity, file_hash, session_id)
        if force:
            self.store.force(self.identity)
        self.hits = 0

    def _identity(self) -> tuple[str, str]:
        try:
            return build_identity(
                self.path,
                settings=self.settings(),
                runtime=runtime_snapshot(self.slots, self.db_path),
                manifest=self.manifest,
                key=self.key,
            )
        except CheckpointError:
            raise
        except Exception:
            raise CheckpointError("checkpoint configuration unavailable") from None

    def ensure_current(self) -> None:
        if self._identity()[0] != self.identity:
            raise CheckpointError("checkpoint inputs changed; restart review")

    @staticmethod
    def clause_hash(clause: Any) -> str:
        return hashlib.sha256(clause.text.encode()).hexdigest()

    def load(self, clause: Any, step: str, category: str) -> ClauseAssessment | None:
        self.ensure_current()
        blob = self.store.get_step(
            self.identity, clause.id, self.clause_hash(clause), category, step
        )
        if blob is None:
            return None
        assessment = self.codec.decode(
            blob,
            clause_id=clause.id,
            category=category,
            text=clause.text,
            extraction_model=self.slots[0],
            qa_model=self.slots[1] if step == "qa" else self.slots[0],
        )
        if assessment is None:
            logger.warning("checkpoint payload invalid; recomputing step")
        else:
            self.hits += 1
        return assessment

    def begin(self, clause: Any, step: str, category: str) -> None:
        self.ensure_current()
        self.store.set_step(
            self.identity, clause.id, self.clause_hash(clause), category, step, "running"
        )

    def save(self, clause: Any, step: str, assessment: ClauseAssessment) -> None:
        self.ensure_current()
        status = "completed" if assessment.error is None else "failed"
        payload = self.codec.encode(assessment) if status == "completed" else None
        self.store.set_step(
            self.identity,
            clause.id,
            self.clause_hash(clause),
            assessment.playbook_category,
            step,
            status,
            payload,
        )

    def finish(self, complete: bool) -> None:
        self.ensure_current()
        self.store.finish(self.identity, complete)
