from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import RLock
from typing import NoReturn

from .agent import (
    AgentCompletedRun,
    AgentFinalizedMemory,
    AgentMemoryError,
    AgentPreparedMemory,
    LocalAgentMemory,
)
from .models import (
    CommitAncestryEvidence,
    MemoryContext,
    MemoryRunMeasurement,
    Mode,
    Trace,
)
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalResult,
    AuthorizedRetrievalScope,
)


@dataclass(frozen=True)
class AuthenticatedAgentPrepareContext:
    """Non-identity retrieval facts accepted by the authenticated façade."""

    mode: Mode
    commit_sha: str
    branch: str | None = None
    prompt_version: str | None = None
    prompt_family: str | None = None
    tool: str | None = None
    tool_schema_version: str | None = None
    model: str | None = None
    model_family: str | None = None
    eval_suite: str | None = None
    task_type: str | None = None
    failure_type: str | None = None
    input_hash: str | None = None

    def bind(self, scope: AuthorizedRetrievalScope) -> MemoryContext:
        if type(scope) is not AuthorizedRetrievalScope:
            _reject_input()
        return MemoryContext(
            mode=self.mode,
            repo=scope.repository_id,
            commit_sha=self.commit_sha,
            branch=self.branch,
            prompt_version=self.prompt_version,
            prompt_family=self.prompt_family,
            tool=self.tool,
            tool_schema_version=self.tool_schema_version,
            model=self.model,
            model_family=self.model_family,
            eval_suite=self.eval_suite,
            task_type=self.task_type,
            failure_type=self.failure_type,
            tenant=scope.tenant_id,
            input_hash=self.input_hash,
        )


class AuthenticatedLocalAgentMemory:
    """Authorize exact server-owned identity context before local retrieval."""

    def __init__(
        self,
        *,
        runtime: LocalAgentMemory,
        authorization_service: AuthenticatedRetrievalService,
        service_context: AuthenticatedServiceContext,
    ) -> None:
        if type(runtime) is not LocalAgentMemory:
            raise TypeError("runtime must be exactly LocalAgentMemory")
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be exactly "
                "AuthenticatedRetrievalService"
            )
        if type(service_context) is not AuthenticatedServiceContext:
            raise TypeError(
                "service_context must be exactly AuthenticatedServiceContext"
            )
        self._runtime = runtime
        self._authorization_service = authorization_service
        self._service_context = service_context
        self._lock = RLock()
        self._request_authorizations: dict[str, str] = {}
        self._pending_requests: set[str] = set()
        self._decision_authorizations: dict[str, str] = {}

    @property
    def service_context(self) -> AuthenticatedServiceContext:
        return self._service_context

    def prepare(
        self,
        trace: Trace,
        context: AuthenticatedAgentPrepareContext,
        *,
        task: str,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> AuthorizedRetrievalResult[AgentPreparedMemory]:
        if (
            type(trace) is not Trace
            or type(context) is not AuthenticatedAgentPrepareContext
        ):
            _reject_input()

        def retrieve(scope: AuthorizedRetrievalScope) -> AgentPreparedMemory:
            canonical_trace = replace(
                trace,
                repo=scope.repository_id,
                tenant=scope.tenant_id,
            )
            return self._runtime.prepare(
                canonical_trace,
                context.bind(scope),
                task=task,
                query=query,
                semantic_scores=semantic_scores,
                max_candidates=max_candidates,
                minimum_score=minimum_score,
                context_summary=context_summary,
                commit_ancestry=commit_ancestry,
            )

        result = self._authorization_service.authorize_retrieval(
            self._service_context,
            retrieve,
        )
        with self._lock:
            self._request_authorizations[result.value.request_id] = (
                result.decision.authorization_event_id
            )
            self._pending_requests.add(result.value.request_id)
        return result

    def finalize(
        self,
        request_id: str,
        decision_payload: str | Mapping[str, object],
    ) -> AgentFinalizedMemory:
        with self._lock:
            if type(request_id) is not str:
                _reject_operation("finalize")
            authorization_event_id = self._request_authorizations.get(
                request_id
            )
            if authorization_event_id is None:
                _reject_operation("finalize")
            try:
                finalized = self._runtime.finalize(
                    request_id,
                    decision_payload,
                )
            except AgentMemoryError:
                finalized = None
            if finalized is None:
                _reject_operation("finalize")
            self._pending_requests.discard(request_id)
            self._decision_authorizations[finalized.decision_id] = (
                authorization_event_id
            )
            return finalized

    def complete(
        self,
        decision_id: str,
        measurement: MemoryRunMeasurement,
    ) -> AgentCompletedRun:
        with self._lock:
            if type(decision_id) is not str:
                _reject_operation("complete")
            if decision_id not in self._decision_authorizations:
                _reject_operation("complete")
            try:
                completed = self._runtime.complete(decision_id, measurement)
            except AgentMemoryError:
                completed = None
            if completed is None:
                _reject_operation("complete")
            return completed

    def cancel(self, request_id: str) -> None:
        with self._lock:
            if type(request_id) is not str:
                _reject_operation("cancel")
            if request_id not in self._pending_requests:
                _reject_operation("cancel")
            try:
                self._runtime.cancel(request_id)
            except AgentMemoryError:
                cancelled = False
            else:
                cancelled = True
            if not cancelled:
                _reject_operation("cancel")
            self._pending_requests.remove(request_id)
            del self._request_authorizations[request_id]


def _reject_input() -> NoReturn:
    raise AuthenticatedServiceV3Error(
        "TBM_AUTHENTICATED_AGENT_INPUT_INVALID",
        "authenticated agent input is invalid",
    )


def _reject_operation(operation: str) -> NoReturn:
    raise AuthenticatedServiceV3Error(
        "TBM_AUTHENTICATED_AGENT_OPERATION_FAILED",
        f"authenticated agent {operation} failed",
    )
