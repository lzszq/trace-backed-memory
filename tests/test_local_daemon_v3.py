from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import time

import pytest

import trace_backed_memory as tbm
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
)
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
    DurableSQLiteRuntime,
)
from trace_backed_memory.local_daemon_v3 import (
    LOCAL_DAEMON_CONTRACT_VERSION,
    LOCAL_DAEMON_DATABASE_NAME,
    LOCAL_DAEMON_LOCK_NAME,
    DurableLocalWorkerLoop,
    LocalDaemonV3Error,
    LocalDaemonWorkerConfiguration,
    local_daemon_lock,
    prepare_local_database,
    prepare_local_state_directory,
    verify_local_database_target,
)


def _symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path, target_is_directory=True)
    except NotImplementedError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    except OSError as error:
        if error.errno not in {
            errno.EACCES,
            errno.ENOSYS,
            errno.EPERM,
            getattr(errno, "ENOTSUP", errno.EPERM),
        } and getattr(error, "winerror", None) != 1314:
            raise
        pytest.skip(f"symbolic links are unavailable: {error}")


def _file_symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path)
    except NotImplementedError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    except OSError as error:
        if error.errno not in {
            errno.EACCES,
            errno.ENOSYS,
            errno.EPERM,
            getattr(errno, "ENOTSUP", errno.EPERM),
        } and getattr(error, "winerror", None) != 1314:
            raise
        pytest.skip(f"symbolic links are unavailable: {error}")


def _complete_one(
    runtime: DurableSQLiteRuntime,
    context: tbm.AuthenticatedServiceContext,
) -> str:
    prepared_response = runtime.dispatcher.prepare(
        context,
        _prepare_request(),
    )
    prepared = runtime.sessions.get(
        prepared_response["result"]["session"]["session_id"]
    )
    evaluation = runtime.evidence_repository.load_evaluation(
        prepared.system_gate_evaluation_id
    )
    runtime.dispatcher.decide(
        context,
        _provider_context(),
        _decide_request(prepared, evaluation),
    )
    decided = runtime.sessions.get(prepared.session_id)
    runtime.dispatcher.finalize(
        context,
        DurableFinalizeRequest(
            session_id=decided.session_id,
            expected_session_version=decided.version,
        ),
    )
    finalized = runtime.sessions.get(decided.session_id)
    runtime.dispatcher.start(
        context,
        DurableStartRequest(
            session_id=finalized.session_id,
            expected_session_version=finalized.version,
        ),
    )
    executing = runtime.sessions.get(finalized.session_id)
    completion = _completion(executing)
    response = runtime.dispatcher.complete(
        context,
        EVALUATOR_CONTEXT,
        DurableCompleteRequest(
            session_id=completion.session_id,
            expected_session_version=completion.expected_version,
            result=completion.result,
            evidence_artifact_sha256s=list(
                completion.evidence_artifact_sha256s
            ),
            output_sha256=completion.output_sha256,
            latency_ms=completion.latency_ms,
            cost_usd=completion.cost_usd,
        ),
    )
    return response["result"]["outbox_event"]["event_id"]


def test_local_state_directory_and_database_are_owner_controlled(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    database = prepare_local_database(
        state_directory,
        initialize=True,
    )

    assert state_directory == tmp_path / ".tbm"
    assert database == state_directory / LOCAL_DAEMON_DATABASE_NAME
    if os.name != "nt":
        assert stat.S_IMODE(os.lstat(state_directory).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(database).st_mode) == 0o600

    assert (
        prepare_local_state_directory(state_directory)
        == state_directory
    )
    assert (
        prepare_local_database(state_directory, initialize=False)
        == database
    )


def test_local_state_rejects_directory_aliases(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-state"
    target.mkdir()
    if os.name != "nt":
        target.chmod(0o700)
    alias = tmp_path / "state-alias"
    _symlink_or_skip(alias, target)
    with pytest.raises(
        LocalDaemonV3Error,
        match="must be a real directory",
    ):
        prepare_local_state_directory(alias)


def test_local_state_rejects_unsafe_database_links(
    tmp_path: Path,
) -> None:
    target = prepare_local_state_directory(
        tmp_path / "real-state",
        create=True,
    )
    database = target / LOCAL_DAEMON_DATABASE_NAME
    unrelated = tmp_path / "unrelated.sqlite3"
    unrelated.write_bytes(b"")
    os.link(unrelated, database)
    with pytest.raises(
        LocalDaemonV3Error,
        match="single-link regular file",
    ):
        prepare_local_database(target, initialize=False)


def test_local_database_revalidation_rejects_replaced_inode(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    database = prepare_local_database(
        state_directory,
        initialize=True,
    )
    expected = verify_local_database_target(database)
    moved = state_directory / "moved.sqlite3"
    database.rename(moved)
    database.write_bytes(b"")
    if os.name != "nt":
        database.chmod(0o600)

    with pytest.raises(LocalDaemonV3Error, match="identity changed"):
        verify_local_database_target(
            database,
            expected_stat=expected,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are required")
def test_local_state_rejects_group_or_other_access(tmp_path: Path) -> None:
    state_directory = tmp_path / ".tbm"
    state_directory.mkdir(mode=0o755)
    state_directory.chmod(0o755)

    with pytest.raises(
        LocalDaemonV3Error,
        match="owner-only",
    ):
        prepare_local_state_directory(state_directory)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are required")
def test_local_state_rejects_replaceable_ancestor(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)

    with pytest.raises(
        LocalDaemonV3Error,
        match="ancestry is unsafe",
    ):
        prepare_local_state_directory(
            unsafe_parent / ".tbm",
            create=True,
        )


def test_local_daemon_lock_is_single_instance_and_recovers(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    with local_daemon_lock(state_directory):
        assert (state_directory / LOCAL_DAEMON_LOCK_NAME).exists()
        with pytest.raises(LocalDaemonV3Error) as raised:
            with local_daemon_lock(state_directory):
                raise AssertionError("second daemon acquired the lock")
        assert raised.value.code == "TBM_LOCAL_DAEMON_ALREADY_RUNNING"

    with local_daemon_lock(state_directory):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are required")
def test_local_daemon_lock_rejects_non_owner_only_mode(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    lock_path = state_directory / LOCAL_DAEMON_LOCK_NAME
    lock_path.write_bytes(b"0")
    lock_path.chmod(0o666)

    with pytest.raises(LocalDaemonV3Error) as raised:
        with local_daemon_lock(state_directory):
            raise AssertionError("unsafe lock was accepted")
    assert raised.value.code == "TBM_LOCAL_DAEMON_LOCK_PERMISSIONS"


def test_local_daemon_lock_rejects_hard_link(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    unrelated = tmp_path / "unrelated.lock"
    unrelated.write_bytes(b"0")
    os.link(unrelated, state_directory / LOCAL_DAEMON_LOCK_NAME)

    with pytest.raises(LocalDaemonV3Error) as raised:
        with local_daemon_lock(state_directory):
            raise AssertionError("hard-linked lock was accepted")
    assert raised.value.code == "TBM_LOCAL_DAEMON_LOCK_UNSAFE"


def test_local_daemon_lock_rejects_symbolic_link(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    unrelated = tmp_path / "unrelated.lock"
    unrelated.write_bytes(b"0")
    _file_symlink_or_skip(
        state_directory / LOCAL_DAEMON_LOCK_NAME,
        unrelated,
    )

    with pytest.raises(LocalDaemonV3Error) as raised:
        with local_daemon_lock(state_directory):
            raise AssertionError("symbolic lock was accepted")
    assert raised.value.code == "TBM_LOCAL_DAEMON_LOCK_UNSAFE"


def test_local_worker_recovers_expired_sessions_and_delivers_outbox() -> None:
    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "f" * 64
        )

    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        _complete_one(runtime, context)

        workers = DurableLocalWorkerLoop(
            runtime,
            worker_id="worker_local_daemon_01",
            configuration=LocalDaemonWorkerConfiguration(
                interval_seconds=0.05,
            ),
        )
        status = workers.run_once()
        assert status.contract_version == LOCAL_DAEMON_CONTRACT_VERSION
        assert status.tick_count == 1
        assert status.recovered_session_count == 0
        assert status.recovery_required_session_count == 0
        assert status.superseded_session_count == 0
        assert status.delivered_event_count == 1
        assert status.retry_wait_event_count == 0
        assert status.dead_letter_event_count == 0
        assert status.recovery_required_event_count == 0
        assert status.superseded_event_count == 0
        assert status.last_error_code is None
        assert status.to_dict() == {
            "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
            "running": False,
            "tick_count": 1,
            "recovered_session_count": 0,
            "recovery_required_session_count": 0,
            "superseded_session_count": 0,
            "delivered_event_count": 1,
            "retry_wait_event_count": 0,
            "dead_letter_event_count": 0,
            "recovery_required_event_count": 0,
            "superseded_event_count": 0,
            "last_error_code": None,
        }
        assert len(delivered) == 1

        workers.start()
        deadline = time.monotonic() + 2
        while (
            workers.status().tick_count < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        workers.stop(timeout_seconds=2.0)
        assert workers.status().running is False
        with pytest.raises(
            LocalDaemonV3Error,
            match="already started",
        ):
            workers.start()


def test_local_worker_recovers_one_expired_preparation() -> None:
    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        completion_consumer=lambda _event: (
            tbm.CompletionOutboxConsumerReceipt()
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        request = _prepare_request().model_copy(
            update={"expires_in_seconds": 300}
        )
        prepared = runtime.dispatcher.prepare(context, request)
        session_id = prepared["result"]["session"]["session_id"]
        workers = DurableLocalWorkerLoop(
            runtime,
            worker_id="worker_local_recovery_01",
            configuration=LocalDaemonWorkerConfiguration(),
        )

        clock.advance(seconds=120)
        status = workers.run_once()
        assert status.recovered_session_count == 0
        assert status.recovery_required_session_count == 1
        assert runtime.sessions.get(session_id).status == "prepared"

        clock.advance(seconds=480)
        status = workers.run_once()
        assert status.recovered_session_count == 1
        assert status.recovery_required_session_count == 1
        assert status.delivered_event_count == 0
        assert status.last_error_code is None
        assert runtime.sessions.get(session_id).status == "expired"


def test_local_worker_records_outbox_retry_and_dead_letter() -> None:
    def consume(_event: tbm.CompletionOutboxEvent) -> tbm.CompletionOutboxConsumerReceipt:
        raise RuntimeError("private consumer failure")

    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        event_id = _complete_one(runtime, context)
        workers = DurableLocalWorkerLoop(
            runtime,
            worker_id="worker_local_failure_01",
            configuration=LocalDaemonWorkerConfiguration(
                outbox_retry_delay_seconds=1,
                outbox_max_attempts=2,
            ),
        )

        first = workers.run_once()
        assert first.retry_wait_event_count == 1
        assert first.dead_letter_event_count == 0
        assert first.last_error_code is None
        assert (
            runtime.outbox_repository.get_delivery(event_id).status
            == "retry_wait"
        )

        clock.advance(seconds=2)
        second = workers.run_once()
        assert second.retry_wait_event_count == 1
        assert second.dead_letter_event_count == 1
        assert second.last_error_code is None
        delivery = runtime.outbox_repository.get_delivery(event_id)
        assert delivery.status == "dead_letter"
        assert delivery.last_error_code == (
            "TBM_COMPLETION_OUTBOX_CONSUMER_FAILED"
        )


def test_local_worker_validates_identity_stop_and_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = LocalDaemonWorkerConfiguration()
    with pytest.raises(TypeError, match="runtime"):
        DurableLocalWorkerLoop(  # type: ignore[arg-type]
            object(),
            worker_id="worker_invalid_runtime",
            configuration=configuration,
        )

    dependencies, _context = _dependencies(
        _Clock(),
        completion_consumer=lambda _event: (
            tbm.CompletionOutboxConsumerReceipt()
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        for worker_id in ("", " bad", "bad\nid", "x" * 129):
            with pytest.raises(ValueError, match="worker_id"):
                DurableLocalWorkerLoop(
                    runtime,
                    worker_id=worker_id,
                    configuration=configuration,
                )
        with pytest.raises(TypeError, match="configuration"):
            DurableLocalWorkerLoop(  # type: ignore[arg-type]
                runtime,
                worker_id="worker_invalid_configuration",
                configuration=object(),
            )

        workers = DurableLocalWorkerLoop(
            runtime,
            worker_id="worker_error_status_01",
            configuration=configuration,
        )
        workers.stop()
        for timeout in (0.0, float("nan"), 1):
            with pytest.raises(ValueError, match="stop timeout"):
                workers.stop(timeout_seconds=timeout)  # type: ignore[arg-type]

        def runtime_recovery_failure(
            _runtime: DurableSQLiteRuntime,
            *,
            limit: int,
        ) -> tuple[object, ...]:
            assert limit == 100
            raise DurableRuntimeV3Error(
                "TBM_TEST_RECOVERY_FAILED",
                "private recovery failure",
            )

        monkeypatch.setattr(
            DurableSQLiteRuntime,
            "recover_due",
            runtime_recovery_failure,
        )
        status = workers.run_once()
        assert status.last_error_code == "TBM_TEST_RECOVERY_FAILED"

        def generic_recovery_failure(
            _runtime: DurableSQLiteRuntime,
            *,
            limit: int,
        ) -> tuple[object, ...]:
            assert limit == 100
            raise RuntimeError("private generic recovery failure")

        def runtime_outbox_failure(
            _runtime: DurableSQLiteRuntime,
            **_kwargs: object,
        ) -> tuple[object, ...]:
            raise DurableRuntimeV3Error(
                "TBM_TEST_OUTBOX_FAILED",
                "private outbox failure",
            )

        monkeypatch.setattr(
            DurableSQLiteRuntime,
            "recover_due",
            generic_recovery_failure,
        )
        monkeypatch.setattr(
            DurableSQLiteRuntime,
            "deliver_outbox",
            runtime_outbox_failure,
        )
        status = workers.run_once()
        assert status.last_error_code == "TBM_TEST_OUTBOX_FAILED"

        def generic_outbox_failure(
            _runtime: DurableSQLiteRuntime,
            **_kwargs: object,
        ) -> tuple[object, ...]:
            raise RuntimeError("private generic outbox failure")

        monkeypatch.setattr(
            DurableSQLiteRuntime,
            "deliver_outbox",
            generic_outbox_failure,
        )
        status = workers.run_once()
        assert status.last_error_code == "TBM_LOCAL_DAEMON_OUTBOX_FAILED"

        class StuckThread:
            def join(self, _timeout: float) -> None:
                return

            def is_alive(self) -> bool:
                return True

        workers._thread = StuckThread()  # type: ignore[assignment]
        with pytest.raises(LocalDaemonV3Error, match="did not stop"):
            workers.stop(timeout_seconds=0.1)


@pytest.mark.parametrize(
    "configuration",
    [
        {"interval_seconds": 0.0},
        {"recovery_limit": 0},
        {"outbox_lease_seconds": 86_401},
        {"outbox_retry_delay_seconds": 604_801},
        {"outbox_max_attempts": 1_001},
    ],
)
def test_local_worker_configuration_rejects_unbounded_values(
    configuration: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        LocalDaemonWorkerConfiguration(**configuration)  # type: ignore[arg-type]
