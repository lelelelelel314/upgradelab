"""Independent behavioral contract for the Pydantic v2 validator case."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from pydantic import ValidationError
from pydantic.warnings import PydanticDeprecatedSince20

warnings.simplefilter("error", PydanticDeprecatedSince20)
sys.path.insert(0, str(Path.cwd()))

from account import Account


assert Account(email=" A@B.COM ").model_dump() == {"email": "a@b.com"}
assert "email" in Account.model_json_schema()["required"]

try:
    Account()
except ValidationError:
    pass
else:
    raise AssertionError("missing email was accepted")
