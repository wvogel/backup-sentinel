import unittest

from app.services.api_client import _is_retryable_http


class IsRetryableHttpTests(unittest.TestCase):
    def test_500_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(500))

    def test_502_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(502))

    def test_503_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(503))

    def test_504_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(504))

    def test_429_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(429))

    def test_404_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(404))

    def test_401_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(401))

    def test_400_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(400))

    def test_200_is_not_retryable(self) -> None:
        # Sanity check: success codes aren't retried.
        self.assertFalse(_is_retryable_http(200))


if __name__ == "__main__":
    unittest.main()
