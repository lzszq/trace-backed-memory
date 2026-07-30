from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
)
from trace_backed_memory import durable_http_entry, http_entry
from trace_backed_memory.durable_http_entry import DurableHTTPApplication
from trace_backed_memory.durable_http_server import (
    DurableBearerAuthenticator,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
)


TOKEN = "durable_http_entry_token_" + "a" * 48


def _application() -> DurableHTTPApplication:
    dependencies, context = _dependencies(_Clock())
    return DurableHTTPApplication(
        dependencies,
        lambda _request: DurableHTTPAuthenticatedContexts(
            context,
            provider=_provider_context(),
            evaluator=EVALUATOR_CONTEXT,
        ),
    )


def test_http_entry_routes_only_explicit_durable_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_http_entry, "main", lambda argv: 23)
    assert (
        http_entry.main(
            ["--profile", "durable-v3", "--sqlite", "runtime.sqlite3"]
        )
        == 23
    )
    assert (
        http_entry.main(
            ["--profile=durable-v3", "--sqlite", "runtime.sqlite3"]
        )
        == 23
    )


def test_durable_http_parser_requires_durable_storage() -> None:
    parser = durable_http_entry._build_parser()
    arguments = parser.parse_args(
        [
            "--profile",
            "durable-v3",
            "--sqlite",
            "runtime.sqlite3",
        ]
    )
    assert arguments.profile == "durable-v3"
    assert arguments.port == 8766
    assert arguments.sqlite == Path("runtime.sqlite3")
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "durable-v3"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--profile",
                "durable-v3",
                "--sqlite",
                "runtime.sqlite3",
                "--postgres-env",
                "POSTGRES_DSN",
            ]
        )


def test_durable_http_application_and_factory_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    with pytest.raises(TypeError, match="dependencies"):
        DurableHTTPApplication(
            cast(object, object()),
            application.context_provider,
        )
    with pytest.raises(TypeError, match="context provider"):
        DurableHTTPApplication(
            application.dependencies,
            cast(object, None),
        )

    for path in (
        "",
        " ",
        "missing-colon",
        "too:many:colons",
        "bad-module!:create",
        "trusted.application:bad-name!",
    ):
        with pytest.raises(ValueError, match="MODULE:CALLABLE"):
            durable_http_entry._load_application(path)

    monkeypatch.setattr(
        durable_http_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            nested=SimpleNamespace(create=lambda: application)
        ),
    )
    assert (
        durable_http_entry._load_application(
            "trusted.application:nested.create"
        )
        is application
    )

    monkeypatch.setattr(
        durable_http_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=object()),
    )
    with pytest.raises(ValueError, match="not callable"):
        durable_http_entry._load_application("trusted.application:create")

    monkeypatch.setattr(
        durable_http_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=lambda: object()),
    )
    with pytest.raises(ValueError, match="invalid data"):
        durable_http_entry._load_application("trusted.application:create")

    def missing_module(_name: str) -> object:
        raise ModuleNotFoundError("private module detail")

    monkeypatch.setattr(
        durable_http_entry.importlib,
        "import_module",
        missing_module,
    )
    with pytest.raises(RuntimeError, match="could not be loaded"):
        durable_http_entry._load_application("trusted.application:create")


@pytest.mark.parametrize("value", (None, "", " token", "BAD=NAME", "BAD\x00NAME"))
def test_durable_http_environment_name_is_strict(value: object) -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        durable_http_entry._validate_environment_name(value, "test")
    assert (
        durable_http_entry._validate_environment_name("TEST_ENV", "test")
        == "TEST_ENV"
    )


def test_durable_http_tls_option_validation_and_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "tls_cert": None,
        "tls_key": None,
        "tls_ca": None,
        "require_client_cert": False,
    }
    assert durable_http_entry._tls_context(SimpleNamespace(**base)) is None
    with pytest.raises(ValueError, match="provided together"):
        durable_http_entry._tls_context(
            SimpleNamespace(**{**base, "tls_cert": Path("cert.pem")})
        )
    with pytest.raises(ValueError, match="requires --tls-ca"):
        durable_http_entry._tls_context(
            SimpleNamespace(**{**base, "require_client_cert": True})
        )
    with pytest.raises(ValueError, match="requires --tls-cert"):
        durable_http_entry._tls_context(
            SimpleNamespace(**{**base, "tls_ca": Path("ca.pem")})
        )

    class _TLSContext:
        def __init__(self, protocol: object) -> None:
            self.protocol = protocol
            self.minimum_version: object | None = None
            self.cert_chain: tuple[Path, Path] | None = None
            self.ca: Path | None = None
            self.verify_mode: object | None = None

        def load_cert_chain(self, cert: Path, key: Path) -> None:
            self.cert_chain = (cert, key)

        def load_verify_locations(self, *, cafile: Path) -> None:
            self.ca = cafile

    monkeypatch.setattr(durable_http_entry.ssl, "SSLContext", _TLSContext)
    context = durable_http_entry._tls_context(
        SimpleNamespace(
            tls_cert=Path("cert.pem"),
            tls_key=Path("key.pem"),
            tls_ca=Path("ca.pem"),
            require_client_cert=True,
        )
    )
    assert context.cert_chain == (Path("cert.pem"), Path("key.pem"))
    assert context.ca == Path("ca.pem")
    assert context.verify_mode == durable_http_entry.ssl.CERT_REQUIRED


def test_durable_http_open_runtime_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    sqlite_runtime = durable_http_entry._open_runtime(
        SimpleNamespace(
            sqlite=tmp_path / "durable.sqlite3",
            initialize=True,
            postgres_env=None,
            expose_injection_content=False,
            expose_replay_content=False,
        ),
        application,
    )
    try:
        assert sqlite_runtime.dispatcher.capabilities()["storage_mode"] == "sqlite"
    finally:
        sqlite_runtime.close()

    with pytest.raises(ValueError, match="parent"):
        durable_http_entry._open_runtime(
            SimpleNamespace(
                sqlite=tmp_path / "missing" / "durable.sqlite3",
                initialize=True,
                postgres_env=None,
                expose_injection_content=False,
                expose_replay_content=False,
            ),
            application,
        )

    opened: dict[str, object] = {}

    class _Factory:
        def __init__(self, dependencies: object) -> None:
            assert dependencies is application.dependencies

        def open_postgres(self, conninfo: str, **kwargs: object) -> object:
            opened["conninfo"] = conninfo
            opened["kwargs"] = kwargs
            return "postgres-runtime"

    monkeypatch.setattr(durable_http_entry, "DurableRuntimeFactory", _Factory)
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://trusted/runtime")
    postgres = durable_http_entry._open_runtime(
        SimpleNamespace(
            sqlite=None,
            initialize=False,
            postgres_env="TEST_POSTGRES_DSN",
            expose_injection_content=True,
            expose_replay_content=True,
        ),
        application,
    )
    assert cast(object, postgres) == "postgres-runtime"
    assert opened["conninfo"] == "postgresql://trusted/runtime"
    monkeypatch.delenv("TEST_POSTGRES_DSN")
    with pytest.raises(ValueError, match="missing"):
        durable_http_entry._open_runtime(
            SimpleNamespace(
                sqlite=None,
                initialize=False,
                postgres_env="TEST_POSTGRES_DSN",
                expose_injection_content=False,
                expose_replay_content=False,
            ),
            application,
        )


def test_durable_bearer_authenticator_checks_secret_before_contexts() -> None:
    application = _application()
    calls: list[str] = []

    def contexts(
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        calls.append(request.operation)
        return application.context_provider(request)

    authenticator = DurableBearerAuthenticator(TOKEN, contexts)
    with pytest.raises(ValueError, match="authentication failed"):
        authenticator(
            DurableHTTPAuthenticationRequest(
                operation="health",
                client_ip="127.0.0.1",
                authorization="Bearer wrong-" + "b" * 48,
            )
        )
    assert calls == []
    trusted = authenticator(
        DurableHTTPAuthenticationRequest(
            operation="health",
            client_ip="127.0.0.1",
            authorization=f"Bearer {TOKEN}",
        )
    )
    assert type(trusted) is DurableHTTPAuthenticatedContexts
    assert calls == ["health"]


def test_durable_http_main_owns_runtime_and_server_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    runtime = SimpleNamespace(
        dispatcher=object(),
        closed=False,
    )
    server_state: dict[str, object] = {}

    def close_runtime() -> None:
        runtime.closed = True

    runtime.close = close_runtime

    class _Server:
        def __init__(
            self,
            configuration: object,
            dispatcher: object,
            authenticator: object,
        ) -> None:
            server_state["configuration"] = configuration
            server_state["dispatcher"] = dispatcher
            server_state["authenticator"] = authenticator
            server_state["closed"] = False

        def serve_forever(self, poll_interval: float) -> None:
            assert poll_interval == 0.25
            server_state["served"] = True

        def server_close(self) -> None:
            server_state["closed"] = True

    monkeypatch.setenv("TEST_DURABLE_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(
        durable_http_entry,
        "_load_application",
        lambda _path: application,
    )
    monkeypatch.setattr(
        durable_http_entry,
        "_open_runtime",
        lambda _args, _application: runtime,
    )
    monkeypatch.setattr(
        durable_http_entry,
        "DurableAgentHTTPServer",
        _Server,
    )
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--token-env",
                "TEST_DURABLE_HTTP_TOKEN",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
                "--initialize",
                "--port",
                "0",
            ]
        )
        == 0
    )
    assert server_state["served"] is True
    assert server_state["closed"] is True
    assert runtime.closed is True
    assert server_state["dispatcher"] is runtime.dispatcher
    assert type(server_state["authenticator"]) is DurableBearerAuthenticator


def test_durable_http_main_reads_factory_env_handles_interrupt_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    state = {"runtime_closed": False, "server_closed": False}
    runtime = SimpleNamespace(dispatcher=object())
    runtime.close = lambda: state.__setitem__("runtime_closed", True)

    class _InterruptingServer:
        def __init__(self, *_args: object) -> None:
            pass

        def serve_forever(self, *, poll_interval: float) -> None:
            assert poll_interval == 0.25
            raise KeyboardInterrupt

        def server_close(self) -> None:
            state["server_closed"] = True

    monkeypatch.setenv("TEST_DURABLE_HTTP_TOKEN", TOKEN)
    monkeypatch.setenv(
        durable_http_entry.DURABLE_HTTP_APPLICATION_FACTORY_ENV,
        "trusted.application:create",
    )
    monkeypatch.setattr(
        durable_http_entry,
        "_load_application",
        lambda path: application,
    )
    monkeypatch.setattr(
        durable_http_entry,
        "_open_runtime",
        lambda _args, _application: runtime,
    )
    monkeypatch.setattr(
        durable_http_entry,
        "DurableAgentHTTPServer",
        _InterruptingServer,
    )
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--token-env",
                "TEST_DURABLE_HTTP_TOKEN",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 0
    )
    assert state == {"runtime_closed": True, "server_closed": True}


def test_durable_http_main_reports_missing_configuration_and_closes_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(
        durable_http_entry.DURABLE_HTTP_APPLICATION_FACTORY_ENV,
        raising=False,
    )
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    assert '"category":"input"' in capsys.readouterr().err

    application = _application()
    state = {"closed": False}
    runtime = SimpleNamespace(dispatcher=object())
    runtime.close = lambda: state.__setitem__("closed", True)
    monkeypatch.setenv("TEST_DURABLE_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(
        durable_http_entry,
        "_load_application",
        lambda _path: application,
    )
    monkeypatch.setattr(
        durable_http_entry,
        "_open_runtime",
        lambda _args, _application: runtime,
    )
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--token-env",
                "TEST_DURABLE_HTTP_TOKEN",
                "--host",
                "localhost",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    assert state["closed"] is True


def test_durable_http_startup_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "private-config.py"

    def fail(_path: str) -> DurableHTTPApplication:
        raise OSError(f"could not read {private_path}")

    monkeypatch.setenv("TEST_DURABLE_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(durable_http_entry, "_load_application", fail)
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--token-env",
                "TEST_DURABLE_HTTP_TOKEN",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "TBM_DURABLE_HTTP_STARTUP_FAILED" in error
    assert str(private_path) not in error


def test_durable_http_application_factory_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "private-provider.json"

    def fail() -> DurableHTTPApplication:
        raise ValueError(f"provider config failed at {private_path}")

    monkeypatch.setenv("TEST_DURABLE_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(
        durable_http_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=fail),
    )
    assert (
        durable_http_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--token-env",
                "TEST_DURABLE_HTTP_TOKEN",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "TBM_DURABLE_HTTP_STARTUP_FAILED" in error
    assert str(private_path) not in error


def test_durable_http_rejects_initialize_for_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    args = SimpleNamespace(
        sqlite=None,
        initialize=True,
        postgres_env="TEST_POSTGRES_DSN",
        expose_injection_content=False,
        expose_replay_content=False,
    )
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://example.invalid/db")
    with pytest.raises(ValueError, match="only valid with --sqlite"):
        durable_http_entry._open_runtime(args, application)
