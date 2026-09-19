import warnings
import unittest

from pydantic.warnings import PydanticDeprecatedSince20

warnings.simplefilter("error", PydanticDeprecatedSince20)

from account import Account


class AccountTest(unittest.TestCase):
    def test_normalizes_email(self) -> None:
        self.assertEqual(Account(email="  USER@EXAMPLE.COM ").email, "user@example.com")


if __name__ == "__main__":
    unittest.main()
