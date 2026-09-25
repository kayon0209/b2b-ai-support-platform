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
from typing import Any
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


# SHA-256 of the empty body. SigV4 signs the payload hash for every request,
# including the ones with no payload at all - DELETE, HEAD and the bucket
# create - so this is a constant rather than a per-call computation.
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


class StorageValidationError(Exception):
    pass


class ObjectNotFound(StorageValidationError):
    """The key does not exist in the bucket.

    Separate from its parent because the caller's correct response differs:
    an unreachable endpoint is worth retrying, a missing object is not - no
    amount of retrying creates it. Collapsing the two made the ingestion
    worker re-read a version whose upload had failed at the storage step, in a
    tight loop, forever.
    """


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

    def _send(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        *,
        payload_hash: str,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        """One signed request.

        The new operations below (create bucket, delete, head, list) each need a
        signed request, and the signing has to agree with the URL that is
        actually sent - `canonical_query` in `_signature` sorts and encodes,
        so the URL is assembled by the same rule rather than by hand. Writing
        that four times is how one of them ends up signed differently from the
        others, and the difference only appears as a 403 from the server, which
        no string-shaped test can see.
        """
        import httpx

        now = datetime.now(UTC)
        headers = self._base_headers(payload_hash, now)
        if extra_headers:
            headers.update(extra_headers)
        auth, _ = self._signature(method, path, query, headers, payload_hash, now)
        headers["authorization"] = auth

        url = f"{'https' if self._secure else 'http'}://{self.endpoint}{path}"
        if query:
            url += "?" + "&".join(
                f"{quote(k, safe='-._~')}={quote(v, safe='-._~')}" for k, v in sorted(query.items())
            )
        return httpx.request(method, url, content=body, headers=headers, timeout=30.0)

    # --- Public API (sync httpx; storage calls are not latency-critical) ---

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Store bytes under `key`. **The caller owns the content-type policy.**

        This used to call `validate_content_type` itself, which hard-coded the
        knowledge allowlist (pdf, text, office documents) into shared
        infrastructure. That was redundant - `knowledge.service` already
        validates on its own seam, and `upload_object` now does it too - and it
        made the client unable to store any object the knowledge corpus has no
        use for: a case attachment's board photograph, for instance. The
        storage client stores bytes; which bytes are acceptable is the calling
        module's rule.
        """
        import httpx

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
        if resp.status_code == 404:
            raise ObjectNotFound(f"get_object failed: 404 for {key}")
        if resp.status_code >= 300:
            raise StorageValidationError(f"get_object failed: {resp.status_code}")
        return resp.content

    def object_exists(self, key: str) -> bool:
        """Whether the key is present.

        Retention needs this because S3's DELETE is idempotent by design: it
        answers 204 whether or not the object was there. Without a separate
        probe, "I deleted it" and "it was never there" are the same result, and
        a sweep cannot tell a successful erasure from a key that was lost
        before the sweep ran.
        """
        path = f"/{self.bucket}/{quote(key, safe='/-._~')}"
        resp = self._send("HEAD", path, {}, payload_hash=EMPTY_PAYLOAD_HASH)
        status = int(resp.status_code)  # type: ignore[attr-defined]
        if status == 404:
            return False
        if status >= 300:
            raise StorageValidationError(f"object_exists failed: {status} for {key}")
        return True

    def delete_object(self, key: str) -> bool:
        """Remove the bytes. Returns False when the object was already absent.

        Not raising on absence is deliberate. The caller - the retention sweep -
        is idempotent and retrying, and raising would leave a row undeletable
        because its object happened to be gone already. Whether anything was
        actually removed is the return value, so the caller can record it.
        """
        if not self.object_exists(key):
            return False

        path = f"/{self.bucket}/{quote(key, safe='/-._~')}"
        resp = self._send("DELETE", path, {}, payload_hash=EMPTY_PAYLOAD_HASH)
        status = int(resp.status_code)  # type: ignore[attr-defined]
        if status >= 300:
            raise StorageValidationError(f"delete_object failed: {status} for {key}")
        return True

    def list_objects(self, prefix: str = "") -> list[str]:
        """Every key under `prefix`, following continuation tokens.

        Reconciliation is the caller: it walks the bucket looking for objects
        no row refers to. The prefix is the tenant id, so the walk stays inside
        one tenant - an unprefixed walk would enumerate every tenant's objects
        from a job that has no reason to read any of them.

        Pagination is followed rather than assumed away. A single page caps at
        1000 keys, and a tenant with more objects than that would otherwise be
        silently reconciled only in part: the missing ones would stay
        unflagged, which is the failure the job exists to catch.
        """
        keys: list[str] = []
        continuation: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if continuation:
                query["continuation-token"] = continuation
            root = self._read_s3_xml("GET", f"/{self.bucket}", query)

            for node in root.iter():
                if node.tag.rpartition("}")[2] == "Key" and node.text:
                    keys.append(node.text)

            truncated = any(
                node.tag.rpartition("}")[2] == "IsTruncated" and node.text == "true"
                for node in root.iter()
            )
            if not truncated:
                return keys
            continuation = next(
                (
                    node.text
                    for node in root.iter()
                    if node.tag.rpartition("}")[2] == "NextContinuationToken"
                ),
                None,
            )
            if not continuation:
                # Truncated with no token means the endpoint disagrees with
                # itself; stopping is preferable to looping on the same page.
                return keys

    def bucket_versioning_enabled(self) -> bool:
        """Whether the bucket keeps prior versions of an object.

        This is not a curiosity. On a versioned bucket a DELETE writes a delete
        marker and the previous bytes stay stored and readable by `version_id`.
        Measured against MinIO, not assumed: after deleting an object, the
        current version 404s while both earlier versions still return their
        full content.

        That matters because "the current version is gone" is exactly what
        `object_exists` reports and exactly what the erasure pass records as a
        completed erasure. On a versioned bucket that record would be false -
        the tenant's document is still fully recoverable, and the platform would
        have certified otherwise. See `evaluation.pii.erase_expired_objects`,
        which refuses to stamp in that case.
        """
        body = self._read_s3_xml("GET", f"/{self.bucket}", {"versioning": ""})
        for node in body.iter():
            if node.tag.rpartition("}")[2] == "Status" and node.text:
                return str(node.text).strip() == "Enabled"
        # An absent Status element is what S3 sends for "never configured",
        # which is the unversioned case.
        return False

    def _read_s3_xml(self, method: str, path: str, query: dict[str, str]) -> Any:
        """Signed request, DTD-refused, parsed. Shared by the XML endpoints.

        The DOCTYPE/ENTITY refusal is the same one `list_objects` applies and
        for the same reason: these are endpoint responses, and over plain HTTP
        the response direction is unauthenticated.
        """
        import xml.etree.ElementTree as ET

        resp = self._send(method, path, query, payload_hash=EMPTY_PAYLOAD_HASH)
        status = int(resp.status_code)  # type: ignore[attr-defined]
        if status >= 300:
            raise StorageValidationError(f"{method} {path} failed: {status}")

        raw = bytes(resp.content)  # type: ignore[attr-defined]
        lowered = raw[:4096].lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise StorageValidationError(
                f"{method} {path}: response carried a DTD or entity declaration"
            )
        return ET.fromstring(raw)

    def set_bucket_versioning(self, enabled: bool) -> None:
        """Turn object versioning on or off for this bucket.

        Exists for the *backup* bucket. The live documents bucket must stay
        unversioned - see `bucket_versioning_enabled` for the measurement that
        makes that a correctness requirement rather than a preference - so
        nothing in the retention path calls this.
        """
        body = (
            "<VersioningConfiguration>"
            f"<Status>{'Enabled' if enabled else 'Suspended'}</Status>"
            "</VersioningConfiguration>"
        ).encode()
        resp = self._send(
            "PUT",
            f"/{self.bucket}",
            {"versioning": ""},
            payload_hash=hashlib.sha256(body).hexdigest(),
            body=body,
            extra_headers={"content-type": "application/xml"},
        )
        status = int(resp.status_code)  # type: ignore[attr-defined]
        if status >= 300:
            raise StorageValidationError(
                f"set_bucket_versioning failed: {status} for {self.bucket!r}"
            )

    def ensure_bucket(self) -> bool:
        """Create the bucket if absent. True only when this call created it.

        Runs from more than one place - a migration job and the retention
        worker - so it has to be re-runnable. S3 answers 409
        (BucketAlreadyOwnedByYou) for a bucket this credential already owns,
        which is the ordinary case after the first run and is not a failure.
        """
        resp = self._send("PUT", f"/{self.bucket}", {}, payload_hash=EMPTY_PAYLOAD_HASH, body=b"")
        status = int(resp.status_code)  # type: ignore[attr-defined]
        if status == 409:
            return False
        if status >= 300:
            raise StorageValidationError(f"ensure_bucket failed: {status} for {self.bucket!r}")
        return True

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
