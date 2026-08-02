from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import sys

from .durable_agent_wire_v1 import DURABLE_AGENT_WIRE_PROTOCOL_VERSION
from .durable_mcp_server import (
    DurableMCPTrustedContexts,
    create_durable_mcp_server,
)
from .durable_runtime_v3 import (
    DurablePostgresRuntime,
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableSQLiteRuntime,
)
from .mcp_server import run_stdio_server


DURABLE_MCP_APPLICATION_FACTORY_ENV = (
    "TBM_DURABLE_MCP_APPLICATION_FACTORY"
)


class _StartupInputError(ValueError):
    """Bounded operator-input failure safe to expose at startup."""


@dataclass(frozen=True)
class DurableMCPApplication:
    """Trusted dependencies and identities for one local STDIO process."""

    dependencies: DurableRuntimeDependencies
    contexts: DurableMCPTrustedContexts

    def __post_init__(self) -> None:
        if type(self.dependencies) is not DurableRuntimeDependencies:
            raise TypeError(
                "durable MCP application dependencies are invalid"
            )
        if type(self.contexts) is not DurableMCPTrustedContexts:
            raise TypeError("durable MCP application contexts are invalid")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbm-mcp",
        description=(
            "Run the explicit durable-v3 Trace-backed Memory MCP profile."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("durable-v3",),
        default="durable-v3",
    )
    parser.add_argument(
        "--application-factory",
        help=(
            "Trusted MODULE:CALLABLE returning DurableMCPApplication. "
            f"Defaults to {DURABLE_MCP_APPLICATION_FACTORY_ENV}."
        ),
    )
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--sqlite", type=Path)
    storage.add_argument("--postgres-env", metavar="ENV_NAME")
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Atomically initialize a new unified SQLite v3 database.",
    )
    parser.add_argument(
        "--expose-injection-content",
        action="store_true",
        help="Explicitly allow exact rendered injection content in results.",
    )
    parser.add_argument(
        "--expose-replay-content",
        action="store_true",
        help="Explicitly allow retained replay content in results.",
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


def _load_application(path: str) -> DurableMCPApplication:
    if (
        type(path) is not str
        or not path
        or path.strip() != path
        or path.count(":") != 1
    ):
        raise _StartupInputError(
            "durable MCP application factory must be MODULE:CALLABLE"
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
            "durable MCP application factory must be MODULE:CALLABLE"
        )
    try:
        target: object = importlib.import_module(module_name)
        for part in attribute_name.split("."):
            target = getattr(target, part)
    except Exception as error:
        raise RuntimeError(
            "durable MCP application factory could not be loaded"
        ) from error
    if not callable(target):
        raise _StartupInputError(
            "durable MCP application factory is not callable"
        )
    try:
        application = target()
    except Exception as error:
        raise RuntimeError(
            "durable MCP application factory failed"
        ) from error
    if type(application) is not DurableMCPApplication:
        raise _StartupInputError(
            "durable MCP application factory returned invalid data"
        )
    return application


def _open_runtime(
    args: argparse.Namespace,
    application: DurableMCPApplication,
) -> DurableSQLiteRuntime | DurablePostgresRuntime:
    if (
        args.expose_replay_content
        and not args.expose_injection_content
    ):
        raise _StartupInputError(
            "--expose-replay-content requires "
            "--expose-injection-content"
        )
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
            event_first_commands=True,
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


def _startup_error(error: Exception) -> dict[str, object]:
    if isinstance(error, _StartupInputError):
        category = "input"
        message = str(error)
        retryable = False
    else:
        category = "internal"
        message = "durable MCP service could not be started"
        retryable = True
    return {
        "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
        "error": {
            "code": "TBM_DURABLE_MCP_STARTUP_FAILED",
            "category": category,
            "message": message,
            "operation": "open",
            "retryable": retryable,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    runtime: DurableSQLiteRuntime | DurablePostgresRuntime | None = None
    try:
        factory_path = args.application_factory
        if factory_path is None:
            factory_path = os.environ.get(
                DURABLE_MCP_APPLICATION_FACTORY_ENV
            )
        if factory_path is None:
            raise _StartupInputError(
                "durable MCP application factory is not configured"
            )
        application = _load_application(factory_path)
        runtime = _open_runtime(args, application)
        server = create_durable_mcp_server(
            runtime.dispatcher,
            application.contexts,
        )
    except Exception as error:
        if runtime is not None:
            runtime.close()
        sys.stderr.write(
            json.dumps(
                _startup_error(error),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    try:
        run_stdio_server(server)
    finally:
        runtime.close()
    return 0


__all__ = [
    "DURABLE_MCP_APPLICATION_FACTORY_ENV",
    "DurableMCPApplication",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
