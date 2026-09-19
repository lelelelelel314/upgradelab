import warnings
import unittest

from pydantic.warnings import PydanticDeprecatedSince20

from loader import load_job


class LoaderTest(unittest.TestCase):
    def test_loads_job(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PydanticDeprecatedSince20)
            job = load_job({"name": "sync", "retries": "2"})
        self.assertEqual(job.retries, 2)


if __name__ == "__main__":
    unittest.main()
