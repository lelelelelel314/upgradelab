from __future__ import annotations

import tempfile
import unittest
from importlib.metadata import PackageNotFoundError, version

from upgradelab.benchmark import run_pydantic_pattern_benchmark


def has_pydantic_v2() -> bool:
    try:
        return int(version("pydantic").split(".", 1)[0]) >= 2
    except (PackageNotFoundError, ValueError):
        return False


class PydanticBenchmarkTests(unittest.TestCase):
    @unittest.skipUnless(has_pydantic_v2(), "Pydantic v2 is not installed")
    def test_real_pydantic_v2_migration_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_pydantic_pattern_benchmark(temp_dir)

            self.assertEqual(result.status, "SUCCEEDED")
            self.assertEqual(result.visible_test_exit_code, 0)
            self.assertEqual(result.acceptance_exit_code, 0)
            self.assertTrue(result.patch_sha256)


if __name__ == "__main__":
    unittest.main()
