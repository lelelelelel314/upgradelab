import unittest

from profile import Registration


class RegistrationVisibleTest(unittest.TestCase):
    def test_accepts_valid_username(self):
        self.assertEqual(Registration(username="alice_7").username, "alice_7")


if __name__ == "__main__":
    unittest.main()
