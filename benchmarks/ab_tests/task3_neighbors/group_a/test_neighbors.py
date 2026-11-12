import unittest
from neighbors import NearestNeighbors

class TestNearestNeighbors(unittest.TestCase):
    def test_valid(self):
        nn = NearestNeighbors(n_neighbors=3)
        nn.fit([1, 2, 3, 4, 5])
        res = nn.kneighbors()
        self.assertEqual(len(res), 3)

    def test_float_fails_with_type_error_in_fit(self):
        nn = NearestNeighbors(n_neighbors=3.0)
        with self.assertRaises(TypeError) as ctx:
            nn.fit([1, 2, 3])
        self.assertIn("must be an integer", str(ctx.exception))

    def test_fractional_float_fails_with_type_error(self):
        nn = NearestNeighbors(n_neighbors=3.5)
        with self.assertRaises(TypeError) as ctx:
            nn.fit([1, 2, 3])
        self.assertIn("must be an integer", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
