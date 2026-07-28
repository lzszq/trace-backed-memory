from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache, wraps
import json
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .contracts_v3 import V3ContractError
from .gate_completion_v3 import GateCompletionRequest, GateCompletionResult
from .gate_session_v3 import (
    GateSession,
    GateSessionContractError,
    transition_gate_session,
)
from .outcome_v3 import (
    RUN_OUTCOME_CONTRACT_VERSION,
    OutcomeContractError,
    RunOutcome,
    build_run_outcome,
    dumps_run_outcome,
    loads_run_outcome,
    verify_run_outcome,
)
from .postgres import _load_psycopg
from .postgres_gate_session_v3 import (
    PostgresGateSessionConflictError,
    PostgresGateSessionNotFoundError,
    PostgresGateSessionPersistenceError,
    PostgresGateSessionRepository,
    PostgresGateSessionSchemaError,
)
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_OUTCOME_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_outcome"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL RunOutcome v3 schema is missing or incomplete"
)
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "run_outcomes",
        "run_outcomes_pkey",
        "run_outcomes_session_id_key",
        "schema_metadata",
        "schema_metadata_pkey",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {"reject_immutable_change", "validate_run_outcome_insert"}
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "outcome_metadata_immutable",
        "outcome_metadata_no_truncate",
        "run_outcomes_immutable_change",
        "run_outcomes_no_truncate",
        "run_outcomes_validate_insert",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "outcome_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "outcome_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
        (
            "run_outcomes_immutable_change",
            "run_outcomes",
            "reject_immutable_change",
            27,
        ),
        (
            "run_outcomes_no_truncate",
            "run_outcomes",
            "reject_immutable_change",
            34,
        ),
        (
            "run_outcomes_validate_insert",
            "run_outcomes",
            "validate_run_outcome_insert",
            7,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "run_outcomes_cost_usd_json_check",
        "run_outcomes_descriptor_check",
        "run_outcomes_error_code_check",
        "run_outcomes_error_shape",
        "run_outcomes_evaluator_id_check",
        "run_outcomes_evaluator_version_check",
        "run_outcomes_evidence_artifact_sha256s_json_check",
        "run_outcomes_latency_ms_check",
        "run_outcomes_output_sha256_check",
        "run_outcomes_output_shape",
        "run_outcomes_pkey",
        "run_outcomes_result_check",
        "run_outcomes_run_id_check",
        "run_outcomes_run_outcome_id_check",
        "run_outcomes_session_fkey",
        "run_outcomes_session_id_key",
        "run_outcomes_tool_outputs_sha256_check",
        "run_outcomes_trace_id_check",
        "run_outcomes_usage_decision_id_check",
        "schema_metadata_contract_version_check",
        "schema_metadata_pkey",
        "schema_metadata_schema_version_check",
        "schema_metadata_singleton_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("run_outcomes", "run_outcome_id", "text", "NO", "C"),
        ("run_outcomes", "session_id", "text", "NO", "C"),
        ("run_outcomes", "trace_id", "text", "NO", "C"),
        ("run_outcomes", "run_id", "text", "NO", "C"),
        ("run_outcomes", "usage_decision_id", "text", "NO", "C"),
        ("run_outcomes", "result", "text", "NO", "C"),
        ("run_outcomes", "evaluator_id", "text", "NO", "C"),
        ("run_outcomes", "evaluator_version", "text", "NO", "C"),
        ("run_outcomes", "output_sha256", "text", "YES", "C"),
        ("run_outcomes", "tool_outputs_sha256", "text", "YES", "C"),
        (
            "run_outcomes",
            "evidence_artifact_sha256s_json",
            "text",
            "NO",
            "C",
        ),
        ("run_outcomes", "latency_ms", "integer", "YES", None),
        ("run_outcomes", "cost_usd_json", "text", "NO", "C"),
        ("run_outcomes", "error_code", "text", "YES", "C"),
        (
            "run_outcomes",
            "measured_at",
            "timestamp with time zone",
            "NO",
            None,
        ),
        ("run_outcomes", "descriptor", "text", "NO", "C"),
        ("schema_metadata", "singleton", "boolean", "NO", None),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
    }
)
_FUNCTION_BODY_PATTERN = re.compile(
    r"CREATE FUNCTION\s+"
    r"trace_backed_memory_v3_outcome\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresOutcomeV3Error(V3ContractError):
    """Stable base failure for PostgreSQL outcome/session completion."""


class PostgresOutcomeV3SchemaError(PostgresOutcomeV3Error):
    pass


class PostgresOutcomeV3ConflictError(PostgresOutcomeV3Error):
    pass


class PostgresOutcomeV3NotFoundError(PostgresOutcomeV3Error):
    pass


class PostgresOutcomeV3PersistenceError(PostgresOutcomeV3Error):
    pass


def _synchronized(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        source = read_packaged_resource(
            "schemas/postgres-v3-outcome.sql"
        ).decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresOutcomeV3SchemaError(
            "TBM_POSTGRES_OUTCOME_SCHEMA",
            "could not read canonical PostgreSQL RunOutcome schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresOutcomeV3SchemaError(
            "TBM_POSTGRES_OUTCOME_SCHEMA",
            "canonical PostgreSQL RunOutcome functions are incomplete",
        )
    return bodies


class PostgresOutcomeV3Repository:
    """Atomic PostgreSQL RunOutcome append and GateSession completion."""

    def __init__(
        self,
        connection: object,
        *,
        owns_connection: bool = False,
    ) -> None:
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False
        self._lock = RLock()
        self._gate_sessions = PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
        self._gate_sessions._lock = self._lock

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresOutcomeV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_PERSISTENCE",
                "failed to connect to PostgreSQL RunOutcome storage",
            ) from error
        return cls(connection, owns_connection=True)

    @property
    def gate_sessions(self) -> PostgresGateSessionRepository:
        """Return the shared authority for setup and non-completion transitions."""

        self._require_open()
        return self._gate_sessions

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_CLOSED",
                "PostgreSQL RunOutcome repository is closed",
            )

    @staticmethod
    def _catalog_names(
        cursor: object,
        query: str,
    ) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL RunOutcome catalog has an invalid shape",
            )
        return frozenset(row["name"] for row in rows)

    @staticmethod
    def _schema_drift(detail: str | None = None) -> NoReturn:
        raise PostgresOutcomeV3SchemaError(
            "TBM_POSTGRES_OUTCOME_SCHEMA",
            "PostgreSQL RunOutcome schema definitions do not match"
            + (f": {detail}" if detail else ""),
        )

    def _verify_schema_catalog(self, cursor: object) -> None:
        relations = self._catalog_names(
            cursor,
            """
            SELECT class.relname AS name
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s
              AND class.relkind IN ('r', 'i', 'p')
            """,
        )
        if relations != _EXPECTED_RELATIONS:
            self._schema_drift()

        functions = self._catalog_names(
            cursor,
            """
            SELECT procedure.proname AS name
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = %s
            """,
        )
        if functions != _EXPECTED_FUNCTIONS:
            self._schema_drift()

        triggers = self._catalog_names(
            cursor,
            """
            SELECT trigger.tgname AS name
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS class
              ON class.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s
              AND NOT trigger.tgisinternal
            """,
        )
        if triggers != _EXPECTED_TRIGGERS:
            self._schema_drift()

        cursor.execute(
            """
            SELECT trigger.tgname,
                   relation.relname AS table_name,
                   procedure.proname AS function_name,
                   function_namespace.nspname AS function_schema,
                   trigger.tgtype
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_proc AS procedure
              ON procedure.oid = trigger.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = procedure.pronamespace
            WHERE relation_namespace.nspname = %s
              AND NOT trigger.tgisinternal
            """,
            (_SCHEMA,),
        )
        trigger_rows = cursor.fetchall()
        try:
            trigger_shapes = frozenset(
                (
                    row["tgname"],
                    row["table_name"],
                    row["function_name"],
                    row["tgtype"],
                )
                for row in trigger_rows
                if row["function_schema"] == _SCHEMA
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if (
            len(trigger_shapes) != len(trigger_rows)
            or trigger_shapes != _EXPECTED_TRIGGER_SHAPES
        ):
            self._schema_drift()

        constraints = self._catalog_names(
            cursor,
            """
            SELECT constraint_record.conname AS name
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = constraint_record.connamespace
            WHERE namespace.nspname = %s
              AND constraint_record.contype <> 'n'
            """,
        )
        if constraints != _EXPECTED_CONSTRAINTS:
            missing = sorted(_EXPECTED_CONSTRAINTS - constraints)
            unexpected = sorted(constraints - _EXPECTED_CONSTRAINTS)
            self._schema_drift(
                f"constraint missing={missing[:1]} "
                f"unexpected={unexpected[:1]}"
            )

        cursor.execute(
            """
            SELECT table_name,
                   column_name,
                   data_type,
                   is_nullable,
                   collation_name
            FROM information_schema.columns
            WHERE table_schema = %s
            """,
            (_SCHEMA,),
        )
        column_rows = cursor.fetchall()
        try:
            columns = frozenset(
                (
                    row["table_name"],
                    row["column_name"],
                    row["data_type"],
                    row["is_nullable"],
                    row["collation_name"],
                )
                for row in column_rows
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if columns != _EXPECTED_COLUMNS:
            self._schema_drift()

        cursor.execute(
            """
            SELECT procedure.proname,
                   procedure.proconfig,
                   procedure.prosrc,
                   language.lanname,
                   pg_catalog.pg_get_function_result(procedure.oid)
                       AS result_type
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            JOIN pg_catalog.pg_language AS language
              ON language.oid = procedure.prolang
            WHERE namespace.nspname = %s
            ORDER BY procedure.proname
            """,
            (_SCHEMA,),
        )
        function_rows = cursor.fetchall()
        if len(function_rows) != len(_EXPECTED_FUNCTIONS):
            self._schema_drift()
        expected_bodies = _expected_function_bodies()
        for row in function_rows:
            if (
                not isinstance(row, Mapping)
                or row.get("proname") not in _EXPECTED_FUNCTIONS
                or row.get("proconfig") != ["search_path=pg_catalog"]
                or row.get("lanname") != "plpgsql"
                or row.get("result_type") != "trigger"
                or type(row.get("prosrc")) is not str
                or row["prosrc"].replace("\r\n", "\n").strip()
                != expected_bodies[row["proname"]]
            ):
                self._schema_drift()

        cursor.execute(
            """
            SELECT
                (SELECT count(*)
                 FROM pg_catalog.pg_policy AS policy
                 JOIN pg_catalog.pg_class AS class
                   ON class.oid = policy.polrelid
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s) AS policy_count,
                (SELECT count(*)
                 FROM pg_catalog.pg_rewrite AS rule
                 JOIN pg_catalog.pg_class AS class
                   ON class.oid = rule.ev_class
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s
                   AND rule.rulename <> '_RETURN') AS rule_count,
                (SELECT count(*)
                 FROM pg_catalog.pg_class AS class
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s
                   AND class.relkind NOT IN ('r', 'i', 'p'))
                    AS unsupported_relation_count
            """,
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [
            {
                "policy_count": 0,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }
        ]:
            self._schema_drift()

    def _lock_schema(self, cursor: object, *, for_write: bool) -> None:
        self._gate_sessions._lock_schema(cursor)
        cursor.execute(
            """
            SELECT outcome.schema_version AS outcome_schema_version,
                   outcome.contract_version AS contract_version
            FROM trace_backed_memory_v3_outcome.schema_metadata AS outcome
            WHERE outcome.singleton
            FOR SHARE OF outcome
            """
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL RunOutcome metadata must contain one row",
            )
        if (
            rows[0].get("outcome_schema_version")
            != POSTGRES_OUTCOME_V3_SCHEMA_VERSION
            or rows[0].get("contract_version")
            != RUN_OUTCOME_CONTRACT_VERSION
        ):
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL RunOutcome schema metadata mismatch",
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_outcome.schema_metadata, "
            "trace_backed_memory_v3_outcome.run_outcomes "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)

    @staticmethod
    def _outcome_values(outcome: RunOutcome) -> tuple[object, ...]:
        evidence_json = json.dumps(
            list(outcome.evidence_artifact_sha256s),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        cost_json = json.dumps(
            outcome.cost_usd,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return (
            outcome.run_outcome_id,
            outcome.session_id,
            outcome.trace_id,
            outcome.run_id,
            outcome.usage_decision_id,
            outcome.result,
            outcome.evaluator_id,
            outcome.evaluator_version,
            outcome.output_sha256,
            outcome.tool_outputs_sha256,
            evidence_json,
            outcome.latency_ms,
            cost_json,
            outcome.error_code,
            outcome.measured_at,
            dumps_run_outcome(outcome),
        )

    @classmethod
    def _outcome_from_row(cls, row: Mapping[str, object]) -> RunOutcome:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_PERSISTENCE",
                "PostgreSQL RunOutcome row has an invalid shape",
            )
        try:
            outcome = loads_run_outcome(descriptor)
            measured_at = PostgresGateSessionRepository._timestamp_from_database(
                row["measured_at"]
            )
            stored_values = (
                row["run_outcome_id"],
                row["session_id"],
                row["trace_id"],
                row["run_id"],
                row["usage_decision_id"],
                row["result"],
                row["evaluator_id"],
                row["evaluator_version"],
                row["output_sha256"],
                row["tool_outputs_sha256"],
                row["evidence_artifact_sha256s_json"],
                row["latency_ms"],
                row["cost_usd_json"],
                row["error_code"],
                measured_at,
                descriptor,
            )
        except (
            KeyError,
            OutcomeContractError,
            PostgresGateSessionPersistenceError,
        ) as error:
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_PERSISTENCE",
                "stored PostgreSQL RunOutcome failed validation",
            ) from error
        if stored_values != cls._outcome_values(outcome):
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_PERSISTENCE",
                "PostgreSQL RunOutcome columns do not match descriptor",
            )
        return outcome

    @classmethod
    def _select_outcome(
        cls,
        cursor: object,
        run_outcome_id: str,
    ) -> RunOutcome:
        cursor.execute(
            """
            SELECT run_outcome_id,
                   session_id,
                   trace_id,
                   run_id,
                   usage_decision_id,
                   result,
                   evaluator_id,
                   evaluator_version,
                   output_sha256,
                   tool_outputs_sha256,
                   evidence_artifact_sha256s_json,
                   latency_ms,
                   cost_usd_json,
                   error_code,
                   measured_at,
                   descriptor
            FROM trace_backed_memory_v3_outcome.run_outcomes
            WHERE run_outcome_id = %s
            """,
            (run_outcome_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresOutcomeV3NotFoundError(
                "TBM_POSTGRES_OUTCOME_NOT_FOUND",
                "RunOutcome was not found",
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_PERSISTENCE",
                "PostgreSQL RunOutcome query returned an invalid shape",
            )
        return cls._outcome_from_row(rows[0])

    @classmethod
    def _insert_outcome(
        cls,
        cursor: object,
        outcome: RunOutcome,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_outcome.run_outcomes (
                run_outcome_id,
                session_id,
                trace_id,
                run_id,
                usage_decision_id,
                result,
                evaluator_id,
                evaluator_version,
                output_sha256,
                tool_outputs_sha256,
                evidence_artifact_sha256s_json,
                latency_ms,
                cost_usd_json,
                error_code,
                measured_at,
                descriptor
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            cls._outcome_values(outcome),
        )

    @staticmethod
    def _matches_request(
        outcome: RunOutcome,
        request: GateCompletionRequest,
    ) -> bool:
        return (
            outcome.result == request.result
            and outcome.evaluator_id == request.evaluator_id
            and outcome.evaluator_version == request.evaluator_version
            and outcome.evidence_artifact_sha256s
            == request.evidence_artifact_sha256s
            and outcome.output_sha256 == request.output_sha256
            and outcome.tool_outputs_sha256 == request.tool_outputs_sha256
            and outcome.latency_ms == request.latency_ms
            and outcome.cost_usd == request.cost_usd
            and outcome.error_code == request.error_code
        )

    @_synchronized
    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> GateCompletionResult:
        self._require_open()
        if type(request) is not GateCompletionRequest:
            raise TypeError("request must be exactly GateCompletionRequest")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=True)
                    current = self._gate_sessions._select_current(
                        cursor,
                        request.session_id,
                        for_update=True,
                    )
                    if current.status == "completed":
                        if current.run_outcome_id is None:
                            raise PostgresOutcomeV3PersistenceError(
                                "TBM_POSTGRES_OUTCOME_ORPHANED_SESSION",
                                "completed GateSession has no RunOutcome",
                            )
                        try:
                            existing = self._select_outcome(
                                cursor,
                                current.run_outcome_id,
                            )
                        except PostgresOutcomeV3NotFoundError as error:
                            raise PostgresOutcomeV3PersistenceError(
                                "TBM_POSTGRES_OUTCOME_ORPHANED_SESSION",
                                "completed GateSession has no retained "
                                "RunOutcome",
                            ) from error
                        if not self._matches_request(existing, request):
                            raise PostgresOutcomeV3ConflictError(
                                "TBM_POSTGRES_OUTCOME_COMPLETION_CONFLICT",
                                "GateSession is completed with another outcome",
                            )
                        try:
                            verify_run_outcome(existing, current)
                        except OutcomeContractError as error:
                            raise PostgresOutcomeV3PersistenceError(
                                "TBM_POSTGRES_OUTCOME_ORPHANED_SESSION",
                                "completed GateSession has inconsistent "
                                "RunOutcome linkage",
                            ) from error
                        self._verify_schema_catalog(cursor)
                        return GateCompletionResult(
                            session=current,
                            outcome=existing,
                            inserted=False,
                        )
                    if current.status != "executing":
                        raise PostgresOutcomeV3ConflictError(
                            "TBM_POSTGRES_OUTCOME_SESSION_STATE",
                            "RunOutcome requires an executing GateSession",
                        )
                    measured_at = self._gate_sessions._database_now(
                        cursor,
                        previous=current.updated_at,
                    )
                    self._gate_sessions._require_live_transition(
                        current,
                        measured_at,
                        "completed",
                    )
                    usage_decision_id = cast(
                        str,
                        current.usage_decision_id,
                    )
                    outcome = build_run_outcome(
                        session_id=current.session_id,
                        trace_id=current.trace_id,
                        run_id=current.run_id,
                        usage_decision_id=usage_decision_id,
                        result=request.result,
                        evaluator_id=request.evaluator_id,
                        evaluator_version=request.evaluator_version,
                        evidence_artifact_sha256s=(
                            request.evidence_artifact_sha256s
                        ),
                        measured_at=measured_at,
                        output_sha256=request.output_sha256,
                        tool_outputs_sha256=request.tool_outputs_sha256,
                        latency_ms=request.latency_ms,
                        cost_usd=request.cost_usd,
                        error_code=request.error_code,
                    )
                    completed = transition_gate_session(
                        current,
                        "completed",
                        expected_version=request.expected_version,
                        updated_at=measured_at,
                        run_outcome_id=outcome.run_outcome_id,
                    )
                    verify_run_outcome(outcome, completed)
                    self._gate_sessions._append_revision(
                        cursor,
                        current,
                        completed,
                        request.expected_version,
                    )
                    self._insert_outcome(cursor, outcome)
                    retained_session = self._gate_sessions._select_current(
                        cursor,
                        current.session_id,
                        for_update=False,
                    )
                    retained_outcome = self._select_outcome(
                        cursor,
                        outcome.run_outcome_id,
                    )
                    if (
                        retained_session != completed
                        or retained_outcome != outcome
                    ):
                        raise PostgresOutcomeV3PersistenceError(
                            "TBM_POSTGRES_OUTCOME_READBACK",
                            "RunOutcome completion read-back changed",
                        )
                    verify_run_outcome(retained_outcome, retained_session)
                    self._verify_schema_catalog(cursor)
                    return GateCompletionResult(
                        session=retained_session,
                        outcome=retained_outcome,
                        inserted=True,
                    )
        except (
            GateSessionContractError,
            OutcomeContractError,
            PostgresOutcomeV3ConflictError,
            PostgresOutcomeV3NotFoundError,
            PostgresOutcomeV3PersistenceError,
            PostgresOutcomeV3SchemaError,
        ):
            raise
        except PostgresGateSessionNotFoundError as error:
            raise PostgresOutcomeV3NotFoundError(
                "TBM_POSTGRES_OUTCOME_SESSION_NOT_FOUND",
                "GateSession was not found",
            ) from error
        except PostgresGateSessionConflictError as error:
            raise PostgresOutcomeV3ConflictError(
                "TBM_POSTGRES_OUTCOME_SESSION_CONFLICT",
                "GateSession cannot be completed in its current time window",
            ) from error
        except PostgresGateSessionSchemaError as error:
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL GateSession dependency failed schema validation",
            ) from error
        except PostgresGateSessionPersistenceError as error:
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_DEPENDENCY",
                "PostgreSQL GateSession dependency failed during completion",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to complete PostgreSQL GateSession with RunOutcome",
            )

    @_synchronized
    def get_session(self, session_id: str) -> GateSession:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=False)
                    return self._gate_sessions._select_current(
                        cursor,
                        session_id,
                        for_update=False,
                    )
        except PostgresGateSessionNotFoundError as error:
            raise PostgresOutcomeV3NotFoundError(
                "TBM_POSTGRES_OUTCOME_SESSION_NOT_FOUND",
                "GateSession was not found",
            ) from error
        except PostgresGateSessionSchemaError as error:
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL GateSession dependency failed schema validation",
            ) from error
        except PostgresGateSessionPersistenceError as error:
            raise PostgresOutcomeV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_DEPENDENCY",
                "PostgreSQL GateSession dependency failed during read-back",
            ) from error
        except PostgresOutcomeV3SchemaError:
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load PostgreSQL GateSession for RunOutcome",
            )

    @_synchronized
    def get_outcome(self, run_outcome_id: str) -> RunOutcome:
        self._require_open()
        if type(run_outcome_id) is not str:
            raise ValueError("run_outcome_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=False)
                    return self._select_outcome(cursor, run_outcome_id)
        except (
            PostgresOutcomeV3NotFoundError,
            PostgresOutcomeV3PersistenceError,
            PostgresOutcomeV3SchemaError,
        ):
            raise
        except PostgresGateSessionSchemaError as error:
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                "PostgreSQL GateSession dependency failed schema validation",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load PostgreSQL RunOutcome",
            )

    @staticmethod
    def _raise_database_error(
        error: BaseException,
        message: str,
    ) -> NoReturn:
        if getattr(error, "sqlstate", None) in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresOutcomeV3SchemaError(
                "TBM_POSTGRES_OUTCOME_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise PostgresOutcomeV3PersistenceError(
            "TBM_POSTGRES_OUTCOME_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate_sessions.close()
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresOutcomeV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
