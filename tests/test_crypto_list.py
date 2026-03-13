"""Tests for the crypto_list utility."""

from utils.crypto_list import TOP_200_SYMBOLS, get_top_200_sync


class TestTop200Symbols:
    def test_list_is_not_empty(self):
        assert len(TOP_200_SYMBOLS) > 100

    def test_major_coins_present(self):
        for symbol in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX"]:
            assert symbol in TOP_200_SYMBOLS

    def test_all_uppercase(self):
        for symbol in TOP_200_SYMBOLS:
            assert symbol == symbol.upper()

    def test_no_duplicates(self):
        assert len(TOP_200_SYMBOLS) == len(set(TOP_200_SYMBOLS))


class TestGetTop200Sync:
    def test_returns_list(self):
        result = get_top_200_sync()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_fallback_returns_static_list(self):
        # When event loop is running, should return static list
        result = get_top_200_sync()
        assert "BTC" in result
