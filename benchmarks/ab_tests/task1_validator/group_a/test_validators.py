import unittest
from validators import ASCIIUsernameValidator, UnicodeUsernameValidator

class TestValidators(unittest.TestCase):
    def test_valid(self):
        v = ASCIIUsernameValidator()
        v("john_doe")
        v("user@test.com")

    def test_trailing_newline_rejected(self):
        v = ASCIIUsernameValidator()
        with self.assertRaises(ValueError):
            v("john_doe\n")

    def test_unicode_trailing_newline_rejected(self):
        v = UnicodeUsernameValidator()
        with self.assertRaises(ValueError):
            v("alice\n")

if __name__ == "__main__":
    unittest.main()
