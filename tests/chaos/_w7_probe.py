"""Private W7 chaos harness: fault seams, XDG isolation, a real local SSE server.

Kept as one private module (the W4/W5 precedent: ``tests/fuzz/_state_probe.py`` and
``tests/redteam/_w5_probe.py``) because W7a/W7b/W7c share the same three things and
the chaos tree's ``conftest.py`` is owned by W7a alone (plan section 7).

What lives here:

* :func:`prepare_state` redirects all four XDG roots at a per-test ``tmp_path`` and
  seeds a real migrated database, so no case can touch the developer's trees.
* :func:`make_gateway` partially constructs a real ``Gateway`` over that database —
  the verified seam at ``tests/integration/test_gateway_streaming.py:47``.
* :class:`FaultSeam` is the in-process dispatch seam. It counts every entry (the
  reachability counter) and then hands the call to ``tests.helpers.faults.apply``,
  so the W0 fault table is genuinely exercised, not re-implemented.
* :class:`LocalSSEServer` is the real ``ThreadingTCPServer`` on ``127.0.0.1:0`` the
  plan cites (``tests/integration/test_gateway_streaming.py:27,86``), used by W7b.

The W0 ``Fault`` record carries no ``status_code``, but ``Gateway._classify_error``
reads exactly that attribute (``router.py:436``). The three HTTP-shaped faults
(``rate_limit``/``five_hundred``/``five_hundred_three``) are therefore re-shaped with
a status-carrying error type through :func:`injected`, keeping the W0 row's name and
message. That is a harness correction, recorded in the register, not a product fact.
"""

from __future__ import annotations

import dataclasses
import http.server
import socketserver
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from openreview_cli.app import app
from openreview_cli.gateway.cost import CostTracker
from openreview_cli.gateway.models import Capability, ProviderInfo
from openreview_cli.gateway.router import Gateway
from openreview_cli.gateway.tier_config import TierConfig
from openreview_cli.storage.database import init_database
from tests.helpers import faults

REPO_ROOT = Path(__file__).resolve().parents[2]

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")

CLOUD_PRIMARY = "openai/gpt-4o"
OPENAI_BASE_URL = "https://api.openai.com/v1"
LOCAL_PRIMARY = "ollama/qwen3:8b"
OLLAMA_BASE_URL = "http://localhost:11434/v1"


def cloud_registry() -> dict[str, ProviderInfo]:
    """A registry holding the cloud provider ``CLOUD_PRIMARY`` names."""
    return {
        "openai": ProviderInfo(
            name="openai",
            env_key="OPENAI_API_KEY",
            base_url=OPENAI_BASE_URL,
            is_local=False,
            capabilities=Capability(reasoning=True, context_window=128_000),
        )
    }


def local_registry() -> dict[str, ProviderInfo]:
    """A registry holding one genuine local provider (loopback base_url)."""
    return {
        "ollama": ProviderInfo(
            name="ollama",
            env_key=None,
            auth_required=False,
            base_url=OLLAMA_BASE_URL,
            is_local=True,
            capabilities=Capability(reasoning=True, embedding=True, context_window=32_000),
        )
    }


# ── Status-carrying provider error doubles ──────────────────────────────────


class RateLimitFaultError(Exception):
    """An injected 429 the way litellm delivers it: a ``status_code`` attribute."""

    status_code = 429


class Server500FaultError(Exception):
    """An injected 500 (the recovery layer calls 500 transient)."""

    status_code = 500


class Server503FaultError(Exception):
    """An injected 503 (transient)."""

    status_code = 503


class AuthFaultError(Exception):
    """An injected 401. Permanent — a retry cannot fix a bad credential."""

    status_code = 401


class ModelNotFoundFaultError(Exception):
    """An injected 404. Permanent — a retry cannot conjure a missing model."""

    status_code = 404


# Fault-name -> status-carrying error class, for the W0 rows that are HTTP-shaped.
STATUS_ERRORS: dict[str, type[Exception]] = {
    "rate_limit": RateLimitFaultError,
    "five_hundred": Server500FaultError,
    "five_hundred_three": Server503FaultError,
}

# Two fault classes the W0 transport table has no row for, but sharp edge 9 is
# about exactly these: a permanent auth failure and a missing model.
EXTRA_FAULTS: dict[str, faults.Fault] = {
    "auth": faults.Fault("auth", error=AuthFaultError),
    "not_found": faults.Fault("not_found", error=ModelNotFoundFaultError),
    # ``empty_choices`` never fires: the injected RESPONSE is the fault. litellm
    # returns this shape for a content-filtered or tool-call-only reply, and it is
    # the realistic twin of the W0 ``delay`` record's no-response outcome.
    "empty_choices": faults.Fault("empty_choices", at_call=1_000_000),
}

# Every fault class W7 drives, by name (the W0 table plus the two permanent ones).
ALL_FAULTS: dict[str, faults.Fault] = {**faults.FAULTS, **EXTRA_FAULTS}


def injected(name: str) -> faults.Fault:
    """Return the ``Fault`` to apply for *name*, with a status-carrying error.

    The three HTTP-shaped W0 rows get their ``error`` replaced by a
    ``status_code``-carrying class so ``Gateway._classify_error`` (``router.py:436``)
    can classify them as it classifies a real provider error. Every other row is the
    W0 record, unchanged.
    """
    fault = ALL_FAULTS[name]
    error = STATUS_ERRORS.get(name)
    if error is None:
        return fault
    return dataclasses.replace(fault, error=error)


# ── XDG isolation and a real Gateway ────────────────────────────────────────


@dataclass
class State:
    """The isolated XDG tree a W7 suite drives the gateway against."""

    root: Path
    config_dir: Path
    config_path: Path
    data_dir: Path
    db_path: Path
    auth_path: Path
    log_dir: Path
    output_dir: Path


def config_body(*, tier: str, primary: str, retries: int, slot_fallback: str | None) -> str:
    """Return the ``config.yml`` body a W7 suite drives the gateway against."""
    lines = [
        "privacy:",
        f"  tier: {tier}",
        "gateway:",
        "  models:",
        "    extraction:",
        f"      primary: {primary}",
    ]
    if slot_fallback is not None:
        lines.append(f"      fallback: {slot_fallback}")
    lines += [
        "  fallback:",
        f"    retries: {retries}",
        "    retry_delay: 0.0",
        "    timeout: 5",
    ]
    return "\n".join(lines) + "\n"


def prepare_state(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    tier: str = "performance",
    primary: str = CLOUD_PRIMARY,
    retries: int = 2,
    slot_fallback: str | None = None,
) -> State:
    """Redirect every XDG root at *tmp_path* and seed a config and a database."""
    for var in XDG_VARS:
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("OPENREVIEW_OUTPUT_DIR", raising=False)

    config_dir = tmp_path / "xdg_config_home" / "openreview"
    data_dir = tmp_path / "xdg_data_home" / "openreview"
    log_dir = tmp_path / "xdg_state_home" / "openreview" / "log"
    output_dir = tmp_path / "out"
    for directory in (config_dir, data_dir, log_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.yml"
    config_path.write_text(
        config_body(tier=tier, primary=primary, retries=retries, slot_fallback=slot_fallback),
        encoding="utf-8",
    )

    db_path = data_dir / "openreview.db"
    init_database(db_path)

    return State(
        root=tmp_path,
        config_dir=config_dir,
        config_path=config_path,
        data_dir=data_dir,
        db_path=db_path,
        auth_path=config_dir / "auth.json",
        log_dir=log_dir,
        output_dir=output_dir,
    )


def make_gateway(
    state: State, *, primary: str, tier: str = "performance", retries: int = 2
) -> Gateway:
    """Partially construct a real ``Gateway`` over *state*'s database.

    ``_cost_tracker`` is the REAL tracker (unlike the W5 harness, which used a
    MagicMock), because W7c's whole point is that ``cost_logs`` rows can be read
    back and cross-checked against the egress counter.
    """
    gw = Gateway.__new__(Gateway)
    gw._config = {
        "privacy": {"tier": tier},
        "gateway": {
            "models": {"extraction": {"primary": primary}},
            "fallback": {"retries": retries, "retry_delay": 0.0, "timeout": 5},
        },
    }
    gw._cloud_calls_made = 0
    gw._cost_tracker = CostTracker(state.db_path)
    gw._tier_config = TierConfig.from_config(gw._config)
    gw._auth = {}
    gw._data_path = state.db_path
    gw._slots = None  # type: ignore[attr-defined]
    return gw


def cost_rows(db_path: Path) -> list[tuple[str, str, int]]:
    """Return ``(model, provider, cost_cents)`` for every ``cost_logs`` row."""
    conn = sqlite3.connect(db_path)
    try:
        return [
            (str(row[0]), str(row[1]), int(row[2]))
            for row in conn.execute("SELECT model, provider, cost_cents FROM cost_logs")
        ]
    finally:
        conn.close()


# ── In-process dispatch seam ────────────────────────────────────────────────


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Message(content)


class Usage:
    """A minimal ``litellm`` usage object (``CostTracker.log_call`` reads three fields)."""

    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 20) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class CompletionResponse:
    """Minimal ``litellm`` completion double: only the fields ``chat`` touches."""

    def __init__(self, content: str = "ok", *, usage: Usage | None = None) -> None:
        self.choices = [_Choice(content)]
        self.usage = usage
        self.model = "gpt-4o"


class EmptyChoicesResponse:
    """The real ``ModelResponse`` shape for a filtered/tool-only reply: no choice.

    Carries ``model`` and ``usage`` like a real reply does, so ``CostTracker.log_call``
    prices it normally — which is how a nonzero cost reaches ``cost_logs`` for a call
    that then crashes.
    """

    def __init__(self) -> None:
        self.choices: list[Any] = []
        self.usage = Usage(5, 5)
        self.model = "gpt-4o"


class FaultSeam:
    """The instrumented dispatch seam: count the entry, then apply the W0 fault."""

    def __init__(self, fault: faults.Fault, response: Any = None) -> None:
        self.fault = fault
        self.response = CompletionResponse("ok") if response is None else response
        self.models: list[str] = []
        self._wrapped = faults.apply(fault, self._ok)

    def _ok(self, **kwargs: Any) -> Any:
        return self.response

    def __call__(self, **kwargs: Any) -> Any:
        self.models.append(str(kwargs.get("model")))
        return self._wrapped(**kwargs)

    @property
    def attempts(self) -> int:
        """Every entry into the seam — the reachability and retry-count counter."""
        return len(self.models)

    def install(self, monkeypatch: Any) -> None:
        """Patch ``router.completion`` in-process. No socket is involved."""
        import openreview_cli.gateway.router as router_mod

        monkeypatch.setattr(router_mod, "completion", self)


class FlakyGateway:
    """One fault-injected gateway: the instance, the seam and the isolated state."""

    def __init__(
        self, gw: Gateway, seam: FaultSeam, state: State, fault_name: str, monkeypatch: Any
    ) -> None:
        self.gw = gw
        self.seam = seam
        self.state = state
        self.fault_name = fault_name
        self._monkeypatch = monkeypatch

    def install_registry(self, registry: dict[str, ProviderInfo] | None = None) -> None:
        """Patch the registry and the ``Gateway`` factory the real CLI path imports."""
        import openreview_cli.gateway.router as router_mod

        reg = registry if registry is not None else cloud_registry()
        self._monkeypatch.setattr(router_mod, "load_registry", lambda: reg)
        # ``gateway test`` and ``review/_gateway.py:111-113`` import Gateway lazily,
        # so patching the module attribute intercepts the real command path.
        self._monkeypatch.setattr(router_mod, "Gateway", lambda *args, **kwargs: self.gw)

    def chat(self, slot: str = "extraction", messages: list[dict[str, str]] | None = None) -> str:
        return self.gw.chat(slot, messages or [{"role": "user", "content": "hi"}])


# ── CLI oracle ──────────────────────────────────────────────────────────────


def run_cli(args: list[str]) -> Result:
    """Invoke the real Typer app with *args* through a one-shot ``CliRunner``."""
    return CliRunner().invoke(app, args)


TRACEBACK_TOKEN = "Traceback (most recent call last)"


def assert_clean_failure(result: Result, allowed: frozenset[int], token: str) -> None:
    """The W7 oracle: an allowed exit code, a named component, and no traceback."""
    assert result.exit_code in allowed, (
        f"exit={result.exit_code} (allowed {sorted(allowed)}) output={result.output!r}"
    )
    assert TRACEBACK_TOKEN not in result.output, (
        f"a raw traceback reached the user: {result.output!r}"
    )
    assert token in result.output, (
        f"the message does not name the failing component: {result.output!r}"
    )


# ── A real local SSE server (W7b) ────────────────────────────────────────────

CHUNK = (
    b'data: {"id":"x","object":"chat.completion.chunk",'
    b'"choices":[{"index":0,"delta":{"content":"hi"},'
    b'"finish_reason":null}]}\n\n'
)
CHUNK2 = (
    b'data: {"id":"x","object":"chat.completion.chunk",'
    b'"choices":[{"index":0,"delta":{"content":"!"},'
    b'"finish_reason":"stop"}]}\n\n'
)
DONE = b"data: [DONE]\n\n"
MALFORMED = b"data: {not json at all\n\n"

# Every stream fault W7b needs a real socket for (plan section 10, W7b)...
STREAM_MODES: tuple[str, ...] = (
    "malformed_body",
    "truncated_stream",
    "mid_stream_disconnect",
    "provider_down",
)
# ... plus the positive control: a well-behaved local SSE stream.
HEALTHY_MODE = "healthy"


def _handler_class(mode: str, hits: list[int]) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _write(self, payload: bytes) -> None:
            self.wfile.write(payload)
            self.wfile.flush()

        def _serve(self) -> None:
            hits[0] += 1
            self._headers()
            if mode == "healthy":
                self._write(CHUNK)
                self._write(CHUNK2)
                self._write(DONE)
                return
            if mode == "malformed_body":
                self._write(MALFORMED)
                self._write(DONE)
                return
            if mode == "truncated_stream":
                # A partial SSE frame: no terminating blank line, then close.
                self._write(CHUNK[: len(CHUNK) - 40])
                return
            # mid_stream_disconnect: one complete chunk, then an abrupt close.
            self._write(CHUNK)

        def do_POST(self) -> None:
            self._serve()

        def do_GET(self) -> None:
            self._serve()

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            pass

    return _Handler


@dataclass
class LocalSSEServer:
    """A real ``ThreadingTCPServer`` bound on ``127.0.0.1:0`` serving one stream fault."""

    mode: str

    def __post_init__(self) -> None:
        self.hits: list[int] = []
        self._httpd: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        assert self._port is not None, "server was never started"
        return self._port

    @property
    def base_url(self) -> str:
        """The loopback base URL; valid even after ``stop()`` (needed by W7b)."""
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> LocalSSEServer:
        """Bind port 0 and serve in a daemon thread. Never a fixed port."""
        hits: list[int] = [0]
        handler = _handler_class(self.mode, hits)
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.hits = hits
        self._httpd = httpd
        self._thread = thread
        self._port = int(httpd.server_address[1])
        return self

    def stop(self) -> None:
        """Shut the server down and join its thread. Idempotent."""
        httpd, thread = self._httpd, self._thread
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=10)
        self._httpd = None
        self._thread = None

    def assert_gone(self) -> None:
        """Teardown assertion (plan W7b): the server must really be gone."""
        assert not self.is_running, f"{self.mode} server thread survived teardown"
        assert self._httpd is None, "the socket was not closed"


def streaming_gateway(monkeypatch: Any, tmp_path: Path, server: LocalSSEServer) -> Gateway:
    """A real Gateway pointed at *server*'s loopback port, with short timeouts.

    The gateway is built over a REAL database with a REAL ``CostTracker`` (not the
    W5 MagicMock), because W7b asserts the cost/counter invariants too: a local
    provider must earn no cloud counter and a faulted stream must not log a cost.

    ``monkeypatch`` owns the teardown, so this is a plain builder, not a context
    manager: the W7b suite keeps the server object so it can assert the socket is
    gone in its own teardown.
    """
    import openreview_cli.gateway.router as router_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
    monkeypatch.setattr(
        router_mod,
        "load_registry",
        lambda: {
            "openai": ProviderInfo(
                name="openai",
                base_url=server.base_url,
                is_local=True,
                capabilities=Capability(),
            )
        },
    )
    monkeypatch.setattr(router_mod, "STREAM_READ_TIMEOUT", 5.0)
    monkeypatch.setattr(router_mod, "STREAM_CONNECT_TIMEOUT", 3.0)

    state = prepare_state(monkeypatch, tmp_path, tier="maximum", primary="openai/test-model")
    gw = Gateway.__new__(Gateway)
    gw._config = {"gateway": {"models": {"extraction": {"primary": "openai/test-model"}}}}
    gw._data_path = state.db_path
    gw._cloud_calls_made = 0
    gw._cost_tracker = CostTracker(state.db_path)
    gw._tier_config = TierConfig.from_config({"privacy": {"tier": "maximum"}})
    gw._auth = {}
    return gw


@dataclass
class StreamOutcome:
    """What ``chat_stream`` actually did: event types, chunk text and any escape."""

    events: list[str]
    chunks: list[str]
    caught: BaseException | None

    @property
    def completed(self) -> bool:
        """True when the stream ended with the gateway's own ``done`` event."""
        return self.caught is None and self.events[-1:] == ["done"]


def collect_stream(gw: Gateway) -> StreamOutcome:
    """Drain ``chat_stream``, recording every event type, chunk text and any escape."""
    events: list[str] = []
    chunks: list[str] = []
    caught: BaseException | None = None
    try:
        for event in gw.chat_stream("extraction", [{"role": "user", "content": "hi"}]):
            events.append(event.type)
            if event.type == "chunk" and event.text:
                chunks.append(event.text)
    except BaseException as exc:
        caught = exc
    return StreamOutcome(events=events, chunks=chunks, caught=caught)


def request_hits(server: LocalSSEServer) -> int:
    """How many HTTP requests actually reached the handler (reachability)."""
    return server.hits[0] if server.hits else 0


def port_is_closed(port: int) -> bool:
    """True when nothing is listening on ``127.0.0.1:port`` (the provider-down probe)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return True
    return False


# Re-exported so suites never re-derive the factory callable type.
FlakyGatewayFactory = Callable[..., FlakyGateway]
