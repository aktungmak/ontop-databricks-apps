"""Signed confirmation tokens for prepared VKG actions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING
from dataclasses import asdict

from actions.models import PrepareTokenPayload

if TYPE_CHECKING:
    from config import Settings

_SUPPORTED_VERSION = 2


class PrepareTokenSigner:
    def __init__(self, secret: str, ttl_seconds: int = 600) -> None:
        self._secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> PrepareTokenSigner:
        if not settings.action_confirm_signing_key:
            raise ValueError("VKG_ACTION_CONFIRM_SIGNING_KEY is required for actions")
        return cls(
            settings.action_confirm_signing_key,
            ttl_seconds=settings.action_prepare_ttl_seconds,
        )

    def sign(self, payload: PrepareTokenPayload) -> str:
        if payload.version != _SUPPORTED_VERSION:
            raise ValueError("unsupported payload version")
        encoded_payload = _encode_json(asdict(payload))
        signature = hmac.new(
            self._secret, encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded_payload}.{_encode_bytes(signature)}"

    def verify(self, token: str, now: int | None = None) -> PrepareTokenPayload:
        try:
            encoded_payload, encoded_signature = token.split(".")
            encoded_payload.encode("ascii")
            signature = _decode_bytes(encoded_signature)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("malformed prepare token") from exc

        expected = hmac.new(
            self._secret, encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid prepare token signature")

        try:
            raw_payload = json.loads(_decode_bytes(encoded_payload))
            if not isinstance(raw_payload, dict):
                raise ValueError("payload is not an object")
            payload = PrepareTokenPayload(**raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("malformed prepare token payload") from exc

        if payload.version != _SUPPORTED_VERSION:
            raise ValueError("unsupported payload version")
        if payload.expires_at <= (int(time.time()) if now is None else now):
            raise ValueError("prepare token has expired")
        return payload


def _encode_json(value: dict[str, object]) -> str:
    return _encode_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
