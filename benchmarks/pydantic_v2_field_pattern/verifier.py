"""Independent behavioral contract for the Pydantic v2 field-pattern case."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path.cwd()))
from profile import Registration


schema = Registration.model_json_schema()
assert schema["properties"]["username"]["pattern"] == r"^[a-z][a-z0-9_]+$"
assert Registration(username="alice_7").model_dump() == {"username": "alice_7"}

for invalid in ("Alice", "a!", "12user"):
    try:
        Registration(username=invalid)
    except ValidationError:
        pass
    else:
        raise AssertionError(f"invalid username was accepted: {invalid}")
