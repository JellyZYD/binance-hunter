from __future__ import annotations

import os
from dataclasses import dataclass


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class BinanceCredentials:
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls, required: bool = True) -> "BinanceCredentials | None":
        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if not api_key and not api_secret and not required:
            return None
        if not api_key or not api_secret:
            raise CredentialError(
                "BINANCE_API_KEY and BINANCE_API_SECRET must both be set in the process environment"
            )
        if any(ch.isspace() for ch in api_key) or any(ch.isspace() for ch in api_secret):
            raise CredentialError("Binance credentials must not contain whitespace")
        return cls(api_key=api_key, api_secret=api_secret)

    @property
    def masked_key(self) -> str:
        if len(self.api_key) < 10:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def redact_secret(text: str, credentials: BinanceCredentials | None = None) -> str:
    out = str(text)
    if credentials:
        out = out.replace(credentials.api_key, "<API_KEY>")
        out = out.replace(credentials.api_secret, "<API_SECRET>")
    for marker in ("signature=", "apiKey="):
        start = 0
        while True:
            idx = out.find(marker, start)
            if idx < 0:
                break
            value_start = idx + len(marker)
            value_end = len(out)
            for separator in ("&", " ", "\n", "\r", '"', "'"):
                candidate = out.find(separator, value_start)
                if candidate >= 0:
                    value_end = min(value_end, candidate)
            out = out[:value_start] + "<REDACTED>" + out[value_end:]
            start = value_start + len("<REDACTED>")
    return out
