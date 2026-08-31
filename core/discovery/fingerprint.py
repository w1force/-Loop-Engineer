"""Stable incident fingerprint for deduplication (PRD §7.1 step 3).

Collapses volatile detail (numbers, hex ids, paths, whitespace) so the same class of
failure maps to one fingerprint across occurrences, while different failure classes
stay distinct.
"""

from __future__ import annotations

from hashlib import sha256
import re

from .contracts import Detection

_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_PATH = re.compile(r"(/[^\s:'\"]+)+")
_WS = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    text = message.strip()
    text = _PATH.sub("<path>", text)
    text = _HEX.sub("<hex>", text)
    text = _DIGITS.sub("#", text)
    text = _WS.sub(" ", text)
    return text.lower()[:400]


def compute_fingerprint(
    *, service: str, detection: Detection, tool: str | None = None
) -> str:
    basis = "|".join(
        (
            service,
            detection.matched_rule,
            detection.error_type or "",
            tool or "",
            normalize_message(detection.message),
        )
    )
    return sha256(basis.encode("utf-8")).hexdigest()


__all__ = ["compute_fingerprint", "normalize_message"]
