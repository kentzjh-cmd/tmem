import unittest
from rst_writer import RSTWriter

class TestRSTWriter(unittest.TestCase):
    def test_default(self):
        writer = RSTWriter()
        out = writer.write(["Col1  Col2", "1     2"])
        self.assertEqual(len(out), 4)

    def test_header_rows(self):
        writer = RSTWriter(header_rows=[["col1", "col2"], ["m", "s"]])
        out = writer.write(["1     2"])
        self.assertIn("col1", out[1])
        self.assertIn("m", out[2])

if __name__ == "__main__":
    unittest.main()
