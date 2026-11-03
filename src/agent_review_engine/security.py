from __future__ import annotations

import hashlib
import hmac


def validate_signature(payload: bytes, header: str | None, secret: str) -> bool:
    """Reject missing, malformed or mismatched GitHub HMAC-SHA256 signatures."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    digest = header.removeprefix("sha256=")
    if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
        return False
    calculated = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, digest.lower())
