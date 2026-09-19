"""Stable, evidence-oriented fingerprints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def canonical_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_set_fingerprint(root: Path, relative_paths: list[str]) -> str:
    """Fingerprint only evidence-relevant files in a deterministic order."""
    digest = hashlib.sha256()
    root = root.resolve()
    for relative in sorted(set(relative_paths)):
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes workspace: {relative}")
        digest.update(relative.replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(candidate.read_bytes() if candidate.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()
