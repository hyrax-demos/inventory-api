"""Unit tests for the stock cache key helper.

GET /items/{sku}/stock (app/routes/items.py) is the sole reader/writer of
this key and only ever keys it by SKU. Every writer (reserve_stock,
release_reservation, bulk_adjust) must produce the identical string so an
invalidation actually clears what the GET handler reads.
"""

from app import cache


def test_stock_cache_key_matches_get_handler_format():
    assert cache.stock_cache_key("tenant-a", "w1", sku="WIDGET") == "stock:WIDGET"


def test_stock_cache_key_is_stable_regardless_of_tenant_or_warehouse():
    """Today's format is SKU-only: passing different tenant/warehouse values
    must not change the resulting key, since the GET handler's cache read
    doesn't vary on them either."""
    key_a = cache.stock_cache_key("tenant-a", "w1", sku="WIDGET")
    key_b = cache.stock_cache_key("tenant-b", "w2", sku="WIDGET")
    assert key_a == key_b == "stock:WIDGET"


def test_stock_key_delegates_to_stock_cache_key():
    """The legacy sku-only helper must keep producing the same string as the
    new helper, so existing cached entries stay valid."""
    assert cache.stock_key("WIDGET") == cache.stock_cache_key(sku="WIDGET")
