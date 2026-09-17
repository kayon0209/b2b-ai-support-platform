"""Object storage module: MinIO/S3 with tenant-prefixed keys (ticket 11).

Security rules (docs/security.md):
- Object keys begin with the tenant UUID.
- Pre-signed short-lived URLs for all client-facing access.
- Content-type validation on upload.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

ALLOWED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/json": ".json",
}


class StorageValidationError(Exception):
    pass


@dataclass(frozen=True)
class ObjectKey:
    tenant_id: str
    document_version_id: str
    filename: str

    def to_key(self) -> str:
        """Tenant-prefixed object key: <tenant>/<version>/<filename>."""
        safe = self.filename.replace("/", "_").replace("\\", "_")
        return f"{self.tenant_id}/{self.document_version_id}/{safe}"


def validate_content_type(content_type: str | None) -> str:
    if not content_type or content_type not in ALLOWED_CONTENT_TYPES:
        raise StorageValidationError(f"content type not allowed: {content_type!r}")
    return content_type


class MinioStorage:
    """S3-signature-v4 client subset: put, get, presign, head.

    Implemented without the minio SDK to keep the dependency surface small;
    the signing logic follows the AWS SigV4 spec that MinIO implements.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str = "documents",
        *,
        secure: bool | None = None,
    ) -> None:
        self.endpoint = (endpoint or "localhost:9000").rstrip("/")
        self.access_key = access_key or "minioadmin"
        self.secret_key = secret_key or "minioadmin"
        self.bucket = bucket
        parsed = urlparse(
            f"http{'s' if (secure if secure is not None else False) else ''}://{self.endpoint}"
        )
        self._host_header = parsed.netloc
        self._secure = secure if secure is not None else False

    # --- SigV4 helpers ---

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    def _signature(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: dict[str, str],
        payload_hash: str,
        now: datetime,
    ) -> tuple[str, str]:
        """Return (authorization_header, amz_date) for the request."""
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        region = "us-east-1"
        service = "s3"

        canonical_uri = quote(path, safe="/-._~")
        canonical_query = "&".join(
            f"{quote(k, safe='-._~')}={quote(v, safe='-._~')}" for k, v in sorted(query.items())
        )
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = (
            f"{method}\n{canonical_uri}\n{canonical_query}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        k_date = self._sign(f"AWS4{self.secret_key}".encode(), date_stamp)
        k_region = self._sign(k_date, region)
        k_service = self._sign(k_region, service)
        k_signing = self._sign(k_service, "aws4_request")
        sig = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        )
        return authorization, amz_date

    def _base_headers(self, payload_hash: str, now: datetime) -> dict[str, str]:
        return {
            "host": self._host_header,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
        }

    # --- Public API (sync httpx; storage calls are not latency-critical) ---

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        import httpx

        validate_content_type(content_type)
        now = datetime.now(UTC)
        payload_hash = hashlib.sha256(data).hexdigest()
        path = f"/{self.bucket}/{quote(key, safe='/-._~')}"
        headers = self._base_headers(payload_hash, now)
        headers["content-type"] = content_type
        auth, _ = self._signature("PUT", path, {}, headers, payload_hash, now)
        headers["authorization"] = auth

        scheme = "https" if self._secure else "http"
        resp = httpx.put(
            f"{scheme}://{self.endpoint}{path}",
            content=data,
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code >= 300:
            raise StorageValidationError(f"put_object failed: {resp.status_code}")
        return key

    def get_object(self, key: str) -> bytes:
        import httpx

        now = datetime.now(UTC)
        payload_hash = "UNSIGNED-PAYLOAD"
        path = f"/{self.bucket}/{quote(key, safe='/-._~')}"
        headers = self._base_headers(payload_hash, now)
        auth, _ = self._signature("GET", path, {}, headers, payload_hash, now)
        headers["authorization"] = auth

        scheme = "https" if self._secure else "http"
        resp = httpx.get(f"{scheme}://{self.endpoint}{path}", headers=headers, timeout=30.0)
        if resp.status_code >= 300:
            raise StorageValidationError(f"get_object failed: {resp.status_code}")
        return resp.content

    def presign_get(self, key: str, *, expires_seconds: int = 300) -> str:
        """Pre-signed short-lived GET URL (default 5 minutes)."""
        now = datetime.now(UTC)
        date_stamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        region, service = "us-east-1", "s3"
        scope = f"{date_stamp}/{region}/{service}/aws4_request"

        expires = str(max(60, min(expires_seconds, 3600)))
        canonical_uri = f"/{self.bucket}/{quote(key, safe='/-._~')}"
        # NOT quoted here. `canonical_query` below URL-encodes every value, so
        # pre-quoting the credential double-encodes the "/" separators into
        # "%252F" and the server rejects the request with
        # AuthorizationQueryParametersError. Signing must use the *unencoded*
        # value (the canonical query string is defined over the decoded form),
        # and encoding happens exactly once, at assembly time.
        credential = f"{self.access_key}/{scope}"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": expires,
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = "&".join(
            f"{quote(k, safe='-._~')}={quote(v, safe='-._~')}" for k, v in sorted(query.items())
        )
        canonical_headers = f"host:{self._host_header}\n"
        canonical_request = (
            f"GET\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\nhost\nUNSIGNED-PAYLOAD"
        )
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        k_date = self._sign(f"AWS4{self.secret_key}".encode(), date_stamp)
        k_region = self._sign(k_date, region)
        k_service = self._sign(k_region, service)
        k_signing = self._sign(k_service, "aws4_request")
        sig = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        scheme = "https" if self._secure else "http"
        return f"{scheme}://{self.endpoint}{canonical_uri}?{canonical_query}&X-Amz-Signature={sig}"
