from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import re
from typing import Generic, NoReturn, Protocol, TypeVar, cast

from .artifact_v3 import (
    ArtifactEncryptionProvider,
    ArtifactRetention,
    EncryptedArtifactRecord,
    artifact_aad,
    retention_allows_read,
    verify_plaintext,
)
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .replay_v3 import ContentAddressedArtifact, verify_artifact_content
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalScope,
)


class ArtifactRecordAuthority(Protocol):
    def put(self, record: EncryptedArtifactRecord) -> object: ...
    def load(self, artifact_id: str) -> EncryptedArtifactRecord: ...
    def find(self, artifact_id: str) -> EncryptedArtifactRecord | None: ...


class AuthenticatedArtifactServiceV3Error(AuthenticatedServiceV3Error):
    """Stable, sanitized Artifact Authority service failure."""


@dataclass(frozen=True)
class StoredArtifactResult:
    record: EncryptedArtifactRecord
    inserted: bool


@dataclass(frozen=True)
class AuthorizedArtifactReadResult:
    content: bytes
    authorization_event_id: str


_T = TypeVar("_T")


@dataclass(frozen=True)
class _ArtifactOperationResult(Generic[_T]):
    value: _T | None = None
    error: AuthenticatedArtifactServiceV3Error | None = None


class AuthenticatedArtifactService:
    """Authorize every operation and store only authenticated ciphertext."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        authority: ArtifactRecordAuthority,
        encryption_provider: ArtifactEncryptionProvider,
        clock: Callable[[], str],
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError("authorization_service must be AuthenticatedRetrievalService")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._authorization_service = authorization_service
        self._authority = authority
        self._provider = encryption_provider
        self._clock = clock
        for name in ("provider_id", "algorithm"):
            value = getattr(encryption_provider, name, None)
            if type(value) is not str or not value:
                raise TypeError("encryption provider metadata is invalid")
        if not callable(getattr(encryption_provider, "encrypt", None)) or not callable(
            getattr(encryption_provider, "decrypt", None)
        ):
            raise TypeError("encryption provider is invalid")

    def put(
        self,
        context: AuthenticatedServiceContext,
        artifact: ContentAddressedArtifact,
        content: bytes,
        *,
        retention: ArtifactRetention | None = None,
    ) -> StoredArtifactResult:
        if type(artifact) is not ContentAddressedArtifact or type(content) is not bytes:
            self._reject("TBM_ARTIFACT_INPUT_INVALID", "artifact input is invalid")
        if artifact.encryption_key_id is None:
            self._reject("TBM_ARTIFACT_KEY_REQUIRED", "encrypted artifact requires encryption_key_id")
        if not verify_artifact_content(artifact, content):
            self._reject("TBM_ARTIFACT_CONTENT_MISMATCH", "artifact content does not match descriptor")
        policy = ArtifactRetention() if retention is None else retention
        if type(policy) is not ArtifactRetention:
            self._reject("TBM_ARTIFACT_RETENTION_INVALID", "artifact retention is invalid")

        def do_store(scope: AuthorizedRetrievalScope) -> StoredArtifactResult:
            try:
                existing = self._authority.find(artifact.artifact_id)
            except Exception:
                self._reject("TBM_ARTIFACT_READ_FAILED", "existing artifact could not be checked")
            if existing is not None:
                if (
                    existing.artifact != artifact
                    or existing.tenant_id != scope.tenant_id
                    or existing.repository_id != scope.repository_id
                    or existing.environment_id != scope.environment_id
                    or existing.encryption_provider_id != self._provider.provider_id
                    or existing.encryption_algorithm != self._provider.algorithm
                    or existing.encryption_key_id != artifact.encryption_key_id
                    or existing.retention != policy
                ):
                    self._reject("TBM_ARTIFACT_IMMUTABLE_CONFLICT", "existing artifact conflicts with requested write")
                try:
                    plaintext = self._provider.decrypt(
                        existing.ciphertext,
                        key_id=existing.encryption_key_id,
                        nonce=existing.nonce,
                        aad=existing.aad(),
                    )
                except Exception:
                    self._reject("TBM_ARTIFACT_DECRYPTION_FAILED", "existing artifact decryption failed")
                if plaintext != content or not verify_plaintext(existing, plaintext):
                    self._reject("TBM_ARTIFACT_INTEGRITY_FAILED", "existing artifact plaintext integrity check failed")
                return StoredArtifactResult(record=existing, inserted=False)
            stored_at = self._safe_clock()
            aad = artifact_aad(
                artifact,
                tenant_id=scope.tenant_id,
                repository_id=scope.repository_id,
                environment_id=scope.environment_id,
                write_authorization_event_id=scope.authorization_event_id,
                encryption_provider_id=self._provider.provider_id,
                encryption_algorithm=self._provider.algorithm,
                encryption_key_id=artifact.encryption_key_id or "",
                retention=policy,
                stored_at=stored_at,
            )
            try:
                ciphertext, nonce = self._provider.encrypt(
                    content, key_id=artifact.encryption_key_id or "", aad=aad
                )
                if type(ciphertext) is not bytes or type(nonce) is not bytes:
                    raise ValueError("invalid provider output")
                if ciphertext == content:
                    raise ValueError("provider returned plaintext")
                plaintext = self._provider.decrypt(
                    ciphertext,
                    key_id=artifact.encryption_key_id or "",
                    nonce=nonce,
                    aad=aad,
                )
            except Exception:
                self._reject("TBM_ARTIFACT_ENCRYPTION_FAILED", "artifact encryption failed")
            if plaintext != content:
                self._reject("TBM_ARTIFACT_ENCRYPTION_FAILED", "artifact encryption round-trip failed")
            try:
                record = EncryptedArtifactRecord(
                    artifact=artifact,
                    tenant_id=scope.tenant_id,
                    repository_id=scope.repository_id,
                    environment_id=scope.environment_id,
                    write_authorization_event_id=scope.authorization_event_id,
                    encryption_provider_id=self._provider.provider_id,
                    encryption_algorithm=self._provider.algorithm,
                    encryption_key_id=artifact.encryption_key_id or "",
                    nonce=nonce,
                    ciphertext=ciphertext,
                    ciphertext_sha256="sha256:" + hashlib.sha256(ciphertext).hexdigest(),
                    retention=policy,
                    stored_at=stored_at,
                )
            except Exception:
                self._reject("TBM_ARTIFACT_ENCRYPTION_FAILED", "artifact encryption output was invalid")
            try:
                receipt = self._authority.put(record)
                inserted = getattr(receipt, "artifact_inserted", None)
                if type(inserted) is not bool or getattr(receipt, "artifact_id", None) != artifact.artifact_id:
                    raise ValueError("invalid receipt")
                if self._authority.load(artifact.artifact_id) != record:
                    raise ValueError("read-back mismatch")
            except Exception:
                self._reject("TBM_ARTIFACT_PERSIST_FAILED", "encrypted artifact could not be persisted")
            return StoredArtifactResult(record=record, inserted=inserted)

        def store(scope: AuthorizedRetrievalScope) -> _ArtifactOperationResult[StoredArtifactResult]:
            try:
                return _ArtifactOperationResult(value=do_store(scope))
            except AuthenticatedArtifactServiceV3Error as error:
                return _ArtifactOperationResult(error=error)

        try:
            outcome = self._authorization_service.authorize_permission(
                context, permission="artifact:write", operation=store
            ).value
            return self._unwrap(outcome)
        except AuthenticatedArtifactServiceV3Error:
            raise
        except AuthenticatedServiceV3Error as error:
            raise AuthenticatedArtifactServiceV3Error(error.code, str(error)) from None

    def get(self, context: AuthenticatedServiceContext, artifact_id: str) -> bytes:
        return self.get_with_receipt(context, artifact_id).content

    def get_with_receipt(
        self,
        context: AuthenticatedServiceContext,
        artifact_id: str,
    ) -> AuthorizedArtifactReadResult:
        if (
            type(artifact_id) is not str
            or re.fullmatch(r"artifact_sha256_[0-9a-f]{64}", artifact_id) is None
        ):
            self._reject("TBM_ARTIFACT_INPUT_INVALID", "artifact_id is invalid")

        def do_load(scope: AuthorizedRetrievalScope) -> bytes:
            try:
                record = self._authority.load(artifact_id)
            except Exception:
                self._reject("TBM_ARTIFACT_READ_FAILED", "encrypted artifact could not be loaded")
            if record.artifact.artifact_id != artifact_id:
                self._reject("TBM_ARTIFACT_INTEGRITY_FAILED", "loaded artifact identity does not match request")
            if (
                record.tenant_id != scope.tenant_id
                or record.repository_id != scope.repository_id
                or record.environment_id != scope.environment_id
            ):
                self._reject("TBM_ARTIFACT_SCOPE_REJECTED", "artifact scope was rejected")
            if record.encryption_provider_id != self._provider.provider_id or record.encryption_algorithm != self._provider.algorithm:
                self._reject("TBM_ARTIFACT_PROVIDER_MISMATCH", "artifact encryption provider does not match")
            if not retention_allows_read(record.retention, self._safe_clock()):
                self._reject("TBM_ARTIFACT_RETENTION_EXPIRED", "artifact retention window has expired")
            try:
                plaintext = self._provider.decrypt(
                    record.ciphertext,
                    key_id=record.encryption_key_id,
                    nonce=record.nonce,
                    aad=record.aad(),
                )
            except Exception:
                self._reject("TBM_ARTIFACT_DECRYPTION_FAILED", "artifact decryption failed")
            if not verify_plaintext(record, plaintext):
                self._reject("TBM_ARTIFACT_INTEGRITY_FAILED", "artifact plaintext integrity check failed")
            return plaintext

        def load(scope: AuthorizedRetrievalScope) -> _ArtifactOperationResult[bytes]:
            try:
                return _ArtifactOperationResult(value=do_load(scope))
            except AuthenticatedArtifactServiceV3Error as error:
                return _ArtifactOperationResult(error=error)

        try:
            authorized = self._authorization_service.authorize_permission(
                context, permission="artifact:read", operation=load
            )
            return AuthorizedArtifactReadResult(
                content=self._unwrap(authorized.value),
                authorization_event_id=(
                    authorized.decision.authorization_event_id
                ),
            )
        except AuthenticatedArtifactServiceV3Error:
            raise
        except AuthenticatedServiceV3Error as error:
            raise AuthenticatedArtifactServiceV3Error(error.code, str(error)) from None

    def _safe_clock(self) -> str:
        try:
            value = self._clock()
            if type(value) is not str:
                raise TypeError
            canonical = canonical_rfc3339(value)
            parse_rfc3339(canonical)
            return canonical
        except (TypeError, ValueError):
            self._reject("TBM_ARTIFACT_CLOCK_FAILED", "trusted artifact clock failed")

    @staticmethod
    def _unwrap(outcome: _ArtifactOperationResult[_T]) -> _T:
        if outcome.error is not None:
            raise outcome.error from None
        return cast(_T, outcome.value)

    @staticmethod
    def _reject(code: str, message: str) -> NoReturn:
        raise AuthenticatedArtifactServiceV3Error(code, message) from None
