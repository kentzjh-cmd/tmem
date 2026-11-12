class NearestNeighbors:
    def __init__(self, n_neighbors=5):
        self.n_neighbors = n_neighbors

    def fit(self, X):
        self._check_params()
        self._fit_X = X
        return self

    def _check_params(self):
        if not isinstance(self.n_neighbors, int) or isinstance(self.n_neighbors, bool):
            raise TypeError("n_neighbors must be an integer")
        if self.n_neighbors <= 0:
            raise ValueError("Expected n_neighbors > 0")

    def kneighbors(self, X=None):
        if not isinstance(self.n_neighbors, int):
            raise TypeError(f"slice indices must be integers or None, but got {type(self.n_neighbors).__name__}")
        return self._fit_X[:self.n_neighbors]
