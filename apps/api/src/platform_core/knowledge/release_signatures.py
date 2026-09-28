"""Ed25519 attestation for platform-produced knowledge evaluation results."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import SecretStr

from platform_contracts.release_attestation import (
    ReleaseEvaluationArtifact,
    ReleasePostTestArtifact,
    SignedReleaseEvaluationArtifact,
    SignedReleasePostTestArtifact,
)

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")
_FUTURE_SKEW = timedelta(minutes=5)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReleaseSignatureError(Exception):
    """A signed evaluator result failed a stable trust check."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovedReleaseDataset:
    sha256: str
    approval_ref: str
    reviewer_ids: tuple[uuid.UUID, uuid.UUID]
    object_key: str


def sign_release_evaluation_artifact(
    artifact: ReleaseEvaluationArtifact,
    *,
    private_key: SecretStr,
) -> SignedReleaseEvaluationArtifact:
    """Sign in the worker process using a deployment-managed private key."""
    key_bytes = _decode_key(private_key.get_secret_value())
    signing_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
    signature = signing_key.sign(artifact.canonical_bytes())
    return SignedReleaseEvaluationArtifact(
        artifact=artifact,
        signature_b64url=base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    )


def require_signing_key_matches_trust_root(
    *,
    key_id: str,
    private_key: SecretStr,
    trusted_public_keys: Mapping[str, str],
) -> None:
    """Refuse a costly evaluation before model calls if the worker key is wrong."""
    encoded_trusted_key = trusted_public_keys.get(key_id)
    if encoded_trusted_key is None:
        raise ReleaseSignatureError("EVALUATOR_KEY_NOT_TRUSTED")
    signing_key = Ed25519PrivateKey.from_private_bytes(_decode_key(private_key.get_secret_value()))
    derived_public_key = (
        base64.urlsafe_b64encode(
            signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if not hmac.compare_digest(derived_public_key, encoded_trusted_key):
        raise ReleaseSignatureError("EVALUATOR_KEY_MISMATCH")


def verify_release_evaluation_artifact(
    signed: SignedReleaseEvaluationArtifact,
    *,
    trusted_public_keys: Mapping[str, str],
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> ReleaseEvaluationArtifact:
    """Verify the worker key, payload integrity and acceptance time window."""
    reference_time = now or datetime.now(UTC)
    if reference_time.tzinfo is None or reference_time.utcoffset() != timedelta(0):
        raise ReleaseSignatureError("EVALUATOR_VERIFIER_TIME_INVALID")
    if signed.artifact.issued_at > reference_time + _FUTURE_SKEW:
        raise ReleaseSignatureError("EVALUATOR_ATTESTATION_FROM_FUTURE")
    if max_age_seconds is not None:
        if max_age_seconds <= 0:
            raise ReleaseSignatureError("EVALUATOR_ATTESTATION_WINDOW_INVALID")
        if reference_time - signed.artifact.issued_at > timedelta(seconds=max_age_seconds):
            raise ReleaseSignatureError("EVALUATOR_ATTESTATION_EXPIRED")

    public_key_b64 = trusted_public_keys.get(signed.artifact.key_id)
    if public_key_b64 is None:
        raise ReleaseSignatureError("EVALUATOR_KEY_NOT_TRUSTED")
    public_key = Ed25519PublicKey.from_public_bytes(_decode_key(public_key_b64))
    signature = _decode_signature(signed.signature_b64url)
    try:
        public_key.verify(signature, signed.artifact.canonical_bytes())
    except InvalidSignature as exc:
        raise ReleaseSignatureError("EVALUATOR_SIGNATURE_INVALID") from exc
    return signed.artifact


def sign_release_post_test_artifact(
    artifact: ReleasePostTestArtifact,
    *,
    private_key: SecretStr,
) -> SignedReleasePostTestArtifact:
    """Sign a real post-publish run in the dedicated evaluator worker."""
    key_bytes = _decode_key(private_key.get_secret_value())
    signing_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
    signature = signing_key.sign(artifact.canonical_bytes())
    return SignedReleasePostTestArtifact(
        artifact=artifact,
        signature_b64url=base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    )


def verify_release_post_test_artifact(
    signed: SignedReleasePostTestArtifact,
    *,
    trusted_public_keys: Mapping[str, str],
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> ReleasePostTestArtifact:
    """Verify the same pinned worker trust root for post-publish evidence."""
    reference_time = now or datetime.now(UTC)
    if reference_time.tzinfo is None or reference_time.utcoffset() != timedelta(0):
        raise ReleaseSignatureError("EVALUATOR_VERIFIER_TIME_INVALID")
    if signed.artifact.issued_at > reference_time + _FUTURE_SKEW:
        raise ReleaseSignatureError("EVALUATOR_ATTESTATION_FROM_FUTURE")
    if max_age_seconds is not None:
        if max_age_seconds <= 0:
            raise ReleaseSignatureError("EVALUATOR_ATTESTATION_WINDOW_INVALID")
        if reference_time - signed.artifact.issued_at > timedelta(seconds=max_age_seconds):
            raise ReleaseSignatureError("EVALUATOR_ATTESTATION_EXPIRED")
    public_key_b64 = trusted_public_keys.get(signed.artifact.key_id)
    if public_key_b64 is None:
        raise ReleaseSignatureError("EVALUATOR_KEY_NOT_TRUSTED")
    public_key = Ed25519PublicKey.from_public_bytes(_decode_key(public_key_b64))
    signature = _decode_signature(signed.signature_b64url)
    try:
        public_key.verify(signature, signed.artifact.canonical_bytes())
    except InvalidSignature as exc:
        raise ReleaseSignatureError("EVALUATOR_SIGNATURE_INVALID") from exc
    return signed.artifact


def configured_evaluator_public_keys() -> dict[str, str]:
    """Load public verification keys; an empty setting trusts no evaluator."""
    from platform_core.config import get_settings

    raw = get_settings().knowledge_evaluator_public_keys_json
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseSignatureError("EVALUATOR_TRUST_CONFIG_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ReleaseSignatureError("EVALUATOR_TRUST_CONFIG_INVALID")
    keys: dict[str, str] = {}
    for key_id, encoded_key in parsed.items():
        if (
            not isinstance(key_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,63}", key_id)
            or not isinstance(encoded_key, str)
        ):
            raise ReleaseSignatureError("EVALUATOR_TRUST_CONFIG_INVALID")
        _decode_key(encoded_key)
        keys[key_id] = encoded_key
    return keys


def parse_approved_release_datasets(
    raw: str,
) -> dict[tuple[uuid.UUID, uuid.UUID], ApprovedReleaseDataset]:
    """Parse the deployment-owned allowlist for tenant/space fixed sets."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseSignatureError("EVAL_DATASET_APPROVAL_CONFIG_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ReleaseSignatureError("EVAL_DATASET_APPROVAL_CONFIG_INVALID")
    approved: dict[tuple[uuid.UUID, uuid.UUID], ApprovedReleaseDataset] = {}
    for scope_key, entry in parsed.items():
        if not isinstance(scope_key, str) or not isinstance(entry, dict):
            raise ReleaseSignatureError("EVAL_DATASET_APPROVAL_CONFIG_INVALID")
        try:
            tenant_text, space_text = scope_key.split("/", maxsplit=1)
            scope = (uuid.UUID(tenant_text), uuid.UUID(space_text))
            reviewers = tuple(uuid.UUID(str(value)) for value in entry["approved_by"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseSignatureError("EVAL_DATASET_APPROVAL_CONFIG_INVALID") from exc
        sha256 = entry.get("sha256")
        approval_ref = entry.get("approval_ref")
        object_key = entry.get("object_key")
        if (
            len(reviewers) != 2
            or reviewers[0] == reviewers[1]
            or not isinstance(sha256, str)
            or not _SHA256_PATTERN.fullmatch(sha256)
            or not isinstance(approval_ref, str)
            or not 1 <= len(approval_ref) <= 127
            or not isinstance(object_key, str)
            or not object_key.startswith(f"{scope[0]}/release-datasets/")
            or len(object_key) > 512
            or ".." in object_key
            or "\\" in object_key
            or set(entry) != {"sha256", "approval_ref", "approved_by", "object_key"}
        ):
            raise ReleaseSignatureError("EVAL_DATASET_APPROVAL_CONFIG_INVALID")
        approved[scope] = ApprovedReleaseDataset(
            sha256=sha256,
            approval_ref=approval_ref,
            reviewer_ids=(reviewers[0], reviewers[1]),
            object_key=object_key,
        )
    return approved


def configured_approved_release_datasets() -> dict[
    tuple[uuid.UUID, uuid.UUID], ApprovedReleaseDataset
]:
    from platform_core.config import get_settings

    return parse_approved_release_datasets(
        get_settings().knowledge_evaluator_approved_datasets_json
    )


def _decode_key(value: str) -> bytes:
    if not _KEY_PATTERN.fullmatch(value):
        raise ReleaseSignatureError("EVALUATOR_KEY_FORMAT_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error) as exc:
        raise ReleaseSignatureError("EVALUATOR_KEY_FORMAT_INVALID") from exc
    if len(decoded) != 32:
        raise ReleaseSignatureError("EVALUATOR_KEY_FORMAT_INVALID")
    return decoded


def _decode_signature(value: str) -> bytes:
    if not _SIGNATURE_PATTERN.fullmatch(value):
        raise ReleaseSignatureError("EVALUATOR_SIGNATURE_FORMAT_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(value + "==")
    except (ValueError, binascii.Error) as exc:
        raise ReleaseSignatureError("EVALUATOR_SIGNATURE_FORMAT_INVALID") from exc
    if len(decoded) != 64:
        raise ReleaseSignatureError("EVALUATOR_SIGNATURE_FORMAT_INVALID")
    return decoded


__all__ = [
    "ApprovedReleaseDataset",
    "ReleaseSignatureError",
    "configured_approved_release_datasets",
    "configured_evaluator_public_keys",
    "parse_approved_release_datasets",
    "require_signing_key_matches_trust_root",
    "sign_release_evaluation_artifact",
    "sign_release_post_test_artifact",
    "verify_release_evaluation_artifact",
    "verify_release_post_test_artifact",
]
