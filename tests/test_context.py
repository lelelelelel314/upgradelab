from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from upgradelab.context import FailureContextSelector


class FailureContextSelectorTests(unittest.TestCase):
    def test_selects_trace_file_imports_and_reverse_importers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "app").mkdir()
            (root / "tests").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "app" / "models.py").write_text("class User: pass\n", encoding="utf-8")
            (root / "app" / "service.py").write_text(
                "from app.models import User\n\ndef load(): return User()\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_service.py").write_text(
                "from app.service import load\n",
                encoding="utf-8",
            )

            traced = root / "app" / "service.py"
            manifest = FailureContextSelector(root).select(
                f'  File "{traced}", line 3, in load\n'
            )

            self.assertEqual(manifest.paths()[0], "app/service.py")
            self.assertIn("app/models.py", manifest.paths())
            self.assertIn("tests/test_service.py", manifest.paths())
            self.assertFalse(manifest.truncated)

    def test_enforces_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "large.py"
            source.write_text("x = 1\n" * 100, encoding="utf-8")

            manifest = FailureContextSelector(root, max_bytes=10).select(
                f'File "{source}", line 1, in <module>'
            )

            self.assertEqual(manifest.paths(), ())
            self.assertTrue(manifest.truncated)


if __name__ == "__main__":
    unittest.main()
