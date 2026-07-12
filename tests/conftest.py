from collections.abc import Generator

import pytest

from tests.postgres_support import (
    _POSTGRES_CLEANUP_CALLBACK_ATTR,
    _postgres_server,
    postgres_cluster,
)

pytest_plugins = ["pytester"]


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    cleanup = getattr(item, _POSTGRES_CLEANUP_CALLBACK_ATTR, None)
    try:
        result = yield
    except BaseException as exc:
        if cleanup is not None:
            cleanup(exc)
        raise
    if cleanup is not None:
        cleanup(None)
    return result


__all__ = ["_postgres_server", "postgres_cluster", "pytest_runtest_call"]
