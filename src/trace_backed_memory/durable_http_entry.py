from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import importlib
import json
import os
from pathlib import Path
import ssl
import sys

from .durable_http_server import (
    DurableAgentHTTPServer,
    DurableBearerAuthenticator,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
    DurableHTTPError,
    DurableHTTPServerConfiguration,
)
from .durable_runtime_v3 import (
    DurablePostgresRuntime,
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableSQLiteRuntime,
)


DURABLE_HTTP_APPLICATION_FACTORY_ENV = (
    "TBM_DURABLE_HTTP_APPLICATION_FACTORY"
)
DURABLE_HTTP_TOKEN_ENV = "TBM_DURABLE_HTTP_TOKEN"


class _StartupInputError(ValueError):
    """Bounded operator-input failure safe to expose at startup."""


@dataclass(frozen=True)
class DurableHTTPApplication:
    """Trusted, operator-loaded dependencies for the durable HTTP profile."""

    dependencies: DurableRuntimeDependencies
    context_provider: Callable[
        [DurableHTTPAuthenticationRequest],
        DurableHTTPAuthenticatedContexts,
    ] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.dependencies) is not DurableRuntimeDependencies:
            raise TypeError(
                "durable HTTP application dependencies are invalid"
            )
        if not callable(self.context_provider):
            raise TypeError(
                "durable HTTP application context provider is invalid"
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbm-http",
        description=(
            "Run the explicit durable-v3 Trace-backed Memory HTTP profile."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("durable-v3",),
        default="durable-v3",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--application-factory",
        help=(
            "Trusted MODULE:CALLABLE returning DurableHTTPApplication. "
            f"Defaults to {DURABLE_HTTP_APPLICATION_FACTORY_ENV}."
        ),
    )
    parser.add_argument(
        "--token-env",
        default=DURABLE_HTTP_TOKEN_ENV,
        help="Environment variable containing the durable bearer secret.",
    )
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--sqlite", type=Path)
    storage.add_argument("--postgres-env", metavar="ENV_NAME")
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Atomically initialize a new unified SQLite v3 database.",
    )
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--tls-ca", type=Path)
    parser.add_argument(
        "--require-client-cert",
        action="store_true",
        help="Require a client certificate signed by --tls-ca.",
    )
    parser.add_argument(
        "--expose-injection-content",
        action="store_true",
        help="Explicitly allow exact rendered injection content in responses.",
    )
    parser.add_argument(
        "--expose-replay-content",
        action="store_true",
        help="Explicitly allow retained replay content in responses.",
    )
    return parser


def _validate_environment_name(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "=" in value
        or "\x00" in value
    ):
        raise _StartupInputError(
            f"{label} environment variable name is invalid"
        )
    return value


def _load_application(path: str) -> DurableHTTPApplication:
    if (
        type(path) is not str
        or not path
        or path.strip() != path
        or path.count(":") != 1
    ):
        raise _StartupInputError(
            "durable HTTP application factory must be MODULE:CALLABLE"
        )
    module_name, separator, attribute_name = path.partition(":")
    if (
        not separator
        or not module_name
        or not attribute_name
        or any(not part.isidentifier() for part in module_name.split("."))
        or any(not part.isidentifier() for part in attribute_name.split("."))
    ):
        raise _StartupInputError(
            "durable HTTP application factory must be MODULE:CALLABLE"
        )
    try:
        target: object = importlib.import_module(module_name)
        for part in attribute_name.split("."):
            target = getattr(target, part)
    except Exception as error:
        raise RuntimeError(
            "durable HTTP application factory could not be loaded"
        ) from error
    if not callable(target):
        raise _StartupInputError(
            "durable HTTP application factory is not callable"
        )
    try:
        application = target()
    except Exception as error:
        raise RuntimeError(
            "durable HTTP application factory failed"
        ) from error
    if type(application) is not DurableHTTPApplication:
        raise _StartupInputError(
            "durable HTTP application factory returned invalid data"
        )
    return application


def _tls_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    values = (args.tls_cert, args.tls_key)
    if (values[0] is None) != (values[1] is None):
        raise _StartupInputError(
            "--tls-cert and --tls-key must be provided together"
        )
    if args.require_client_cert and args.tls_ca is None:
        raise _StartupInputError("--require-client-cert requires --tls-ca")
    if args.tls_ca is not None and args.tls_cert is None:
        raise _StartupInputError(
            "--tls-ca requires --tls-cert and --tls-key"
        )
    if args.tls_cert is None:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.tls_cert, args.tls_key)
    if args.tls_ca is not None:
        context.load_verify_locations(cafile=args.tls_ca)
        context.verify_mode = (
            ssl.CERT_REQUIRED
            if args.require_client_cert
            else ssl.CERT_OPTIONAL
        )
    return context


def _open_runtime(
    args: argparse.Namespace,
    application: DurableHTTPApplication,
) -> DurableSQLiteRuntime | DurablePostgresRuntime:
    factory = DurableRuntimeFactory(application.dependencies)
    if args.sqlite is not None:
        database = args.sqlite.resolve(strict=False)
        if not database.parent.is_dir():
            raise _StartupInputError(
                "SQLite database parent directory must exist"
            )
        return factory.open_sqlite(
            database,
            initialize=args.initialize,
            expose_injection_content=args.expose_injection_content,
            expose_replay_content=args.expose_replay_content,
            check_same_thread=False,
        )
    if args.initialize:
        raise _StartupInputError("--initialize is only valid with --sqlite")
    postgres_env = _validate_environment_name(
        args.postgres_env,
        "PostgreSQL",
    )
    conninfo = os.environ.get(postgres_env)
    if conninfo is None or not conninfo.strip():
        raise _StartupInputError(
            "configured PostgreSQL environment variable is missing"
        )
    return factory.open_postgres(
        conninfo,
        expose_injection_content=args.expose_injection_content,
        expose_replay_content=args.expose_replay_content,
    )


def _startup_error(
    error: Exception,
) -> DurableHTTPError:
    if isinstance(error, _StartupInputError):
        return DurableHTTPError(
            "TBM_DURABLE_HTTP_STARTUP_FAILED",
            "input",
            "open",
            str(error),
        )
    return DurableHTTPError(
        "TBM_DURABLE_HTTP_STARTUP_FAILED",
        "internal",
        "open",
        "durable HTTP service could not be started",
        retryable=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    runtime: DurableSQLiteRuntime | DurablePostgresRuntime | None = None
    server: DurableAgentHTTPServer | None = None
    try:
        factory_path = args.application_factory
        if factory_path is None:
            factory_path = os.environ.get(
                DURABLE_HTTP_APPLICATION_FACTORY_ENV
            )
        if factory_path is None:
            raise _StartupInputError(
                "durable HTTP application factory is not configured"
            )
        token_env = _validate_environment_name(args.token_env, "token")
        token = os.environ.get(token_env)
        if token is None:
            raise _StartupInputError(
                "configured durable HTTP token environment variable is missing"
            )
        application = _load_application(factory_path)
        try:
            authenticator = DurableBearerAuthenticator(
                token,
                application.context_provider,
            )
        except (TypeError, ValueError) as error:
            raise _StartupInputError(str(error)) from error
        runtime = _open_runtime(args, application)
        try:
            configuration = DurableHTTPServerConfiguration(
                host=args.host,
                port=args.port,
                tls_context=_tls_context(args),
            )
        except ValueError as error:
            raise _StartupInputError(str(error)) from error
        server = DurableAgentHTTPServer(
            configuration,
            runtime.dispatcher,
            authenticator,
        )
    except Exception as error:
        if server is not None:
            server.server_close()
        if runtime is not None:
            runtime.close()
        message = _startup_error(error)
        sys.stderr.write(
            json.dumps(
                message.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
    return 0


__all__ = [
    "DURABLE_HTTP_APPLICATION_FACTORY_ENV",
    "DURABLE_HTTP_TOKEN_ENV",
    "DurableHTTPApplication",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
