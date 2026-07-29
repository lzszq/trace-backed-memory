from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Protocol

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError, canonical_sha256
from .replay_v3 import ContentAddressedArtifact, verify_artifact_content


ARTIFACT_AUTHORITY_CONTRACT_VERSION = "tbm.artifact-authority.v3"
ARTIFACT_CIPHERTEXT_MAX_BYTES = 64 * 1024 * 1024 + 65536
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


class ArtifactV3ContractError(V3ContractError):
    """Stable failure for encrypted Artifact Authority contracts."""


class ArtifactEncryptionProvider(Protocol):
    """Caller-owned authenticated-encryption or KMS boundary."""

    provider_id: str
    algorithm: str

    def encrypt(
        self, plaintext: bytes, *, key_id: str, aad: bytes
    ) -> tuple[bytes, bytes]: ...

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        key_id: str,
        nonce: bytes,
        aad: bytes,
    ) -> bytes: ...


@dataclass(frozen=True)
class ArtifactRetention:
    retain_until: str | None = None
    legal_hold: bool = False

    def __post_init__(self) -> None:
        if self.retain_until is not None:
            if type(self.retain_until) is not str:
                _invalid("retain_until must be a timestamp or null")
            try:
                value = canonical_rfc3339(self.retain_until)
            except (TypeError, ValueError) as error:
                raise ArtifactV3ContractError(
                    "TBM_ARTIFACT_RETENTION_INVALID",
                    "retain_until must be a canonical RFC 3339 timestamp",
                ) from error
            object.__setattr__(self, "retain_until", value)
        if type(self.legal_hold) is not bool:
            _invalid("legal_hold must be a boolean")


@dataclass(frozen=True)
class EncryptedArtifactRecord:
    artifact: ContentAddressedArtifact
    tenant_id: str
    repository_id: str
    environment_id: str
    write_authorization_event_id: str
    encryption_provider_id: str
    encryption_algorithm: str
    encryption_key_id: str
    nonce: bytes
    ciphertext: bytes
    ciphertext_sha256: str
    retention: ArtifactRetention
    stored_at: str
    contract_version: str = ARTIFACT_AUTHORITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ARTIFACT_AUTHORITY_CONTRACT_VERSION:
            _invalid("contract_version is unsupported")
        if type(self.artifact) is not ContentAddressedArtifact:
            _invalid("artifact must be exactly ContentAddressedArtifact")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.environment_id, "environment_id"),
            (self.write_authorization_event_id, "write_authorization_event_id"),
            (self.encryption_provider_id, "encryption_provider_id"),
            (self.encryption_algorithm, "encryption_algorithm"),
            (self.encryption_key_id, "encryption_key_id"),
        ):
            if type(value) is not str or _ID_RE.fullmatch(value) is None:
                _invalid(f"{name} must be a bounded identifier")
        if self.artifact.encryption_key_id != self.encryption_key_id:
            _invalid("artifact and envelope encryption_key_id must match")
        if type(self.nonce) is not bytes or not self.nonce or len(self.nonce) > 1024:
            _invalid("nonce must contain 1 to 1024 bytes")
        if (
            type(self.ciphertext) is not bytes
            or not self.ciphertext
            or len(self.ciphertext) > ARTIFACT_CIPHERTEXT_MAX_BYTES
        ):
            _invalid("ciphertext has an invalid size")
        if type(self.ciphertext_sha256) is not str or _DIGEST_RE.fullmatch(
            self.ciphertext_sha256
        ) is None:
            _invalid("ciphertext_sha256 must be a SHA-256 digest")
        actual = "sha256:" + hashlib.sha256(self.ciphertext).hexdigest()
        if actual != self.ciphertext_sha256:
            _invalid("ciphertext_sha256 does not match ciphertext")
        if type(self.retention) is not ArtifactRetention:
            _invalid("retention must be exactly ArtifactRetention")
        try:
            object.__setattr__(self, "stored_at", canonical_rfc3339(self.stored_at))
        except (TypeError, ValueError) as error:
            raise ArtifactV3ContractError(
                "TBM_ARTIFACT_RECORD_INVALID",
                "stored_at must be a canonical RFC 3339 timestamp",
            ) from error

    def aad(self) -> bytes:
        return artifact_aad(
            self.artifact,
            tenant_id=self.tenant_id,
            repository_id=self.repository_id,
            environment_id=self.environment_id,
            write_authorization_event_id=self.write_authorization_event_id,
            encryption_provider_id=self.encryption_provider_id,
            encryption_algorithm=self.encryption_algorithm,
            encryption_key_id=self.encryption_key_id,
            retention=self.retention,
            stored_at=self.stored_at,
        )


def artifact_aad(
    artifact: ContentAddressedArtifact,
    *,
    tenant_id: str,
    repository_id: str,
    environment_id: str,
    write_authorization_event_id: str,
    encryption_provider_id: str,
    encryption_algorithm: str,
    encryption_key_id: str,
    retention: ArtifactRetention,
    stored_at: str,
) -> bytes:
    payload = {
        "contract_version": ARTIFACT_AUTHORITY_CONTRACT_VERSION,
        "artifact": artifact.to_dict(),
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "environment_id": environment_id,
        "write_authorization_event_id": write_authorization_event_id,
        "encryption_provider_id": encryption_provider_id,
        "encryption_algorithm": encryption_algorithm,
        "encryption_key_id": encryption_key_id,
        "retention": {
            "retain_until": retention.retain_until,
            "legal_hold": retention.legal_hold,
        },
        "stored_at": canonical_rfc3339(stored_at),
    }
    return canonical_sha256(payload).encode("ascii")


def retention_allows_read(retention: ArtifactRetention, at: str) -> bool:
    if retention.legal_hold or retention.retain_until is None:
        return True
    try:
        return parse_rfc3339(at) <= parse_rfc3339(retention.retain_until)
    except (TypeError, ValueError) as error:
        raise ArtifactV3ContractError(
            "TBM_ARTIFACT_READ_TIME_INVALID", "read time is invalid"
        ) from error


def verify_plaintext(record: EncryptedArtifactRecord, plaintext: bytes) -> bool:
    return type(plaintext) is bytes and verify_artifact_content(record.artifact, plaintext)


def _invalid(message: str) -> None:
    raise ArtifactV3ContractError("TBM_ARTIFACT_RECORD_INVALID", message)
