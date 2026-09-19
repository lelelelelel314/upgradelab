"""Independent behavioral contract for the Pydantic v2 model-validate case."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from pydantic import ValidationError
from pydantic.warnings import PydanticDeprecatedSince20

sys.path.insert(0, str(Path.cwd()))

from loader import load_job


with warnings.catch_warnings():
    warnings.simplefilter("error", PydanticDeprecatedSince20)
    job = load_job({"name": "sync", "retries": "3"})

assert job.model_dump() == {"name": "sync", "retries": 3}

try:
    load_job({"name": "sync", "retries": "never"})
except ValidationError:
    pass
else:
    raise AssertionError("invalid retry count was accepted")
