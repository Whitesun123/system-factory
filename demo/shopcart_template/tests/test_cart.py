import pytest

from shopcart.cart import Cart, bulk_discount_percent, coupon_discount


def test_subtotal_sums_line_items():
    cart = Cart()
    cart.add("pen", 1.5, quantity=4)
    cart.add("book", 10.0)
    assert cart.subtotal() == 16.0


def test_add_rejects_non_positive_quantity():
    cart = Cart()
    with pytest.raises(ValueError):
        cart.add("pen", 1.0, quantity=0)


def test_add_rejects_negative_price():
    cart = Cart()
    with pytest.raises(ValueError):
        cart.add("pen", -1.0)
    cart.add("freebie", 0.0)  # zero price is allowed


def test_small_order_pays_flat_shipping():
    cart = Cart()
    cart.add("pen", 10.0)
    assert cart.shipping() == 4.99
    assert cart.total() == 14.99


def test_large_order_ships_free():
    cart = Cart()
    cart.add("chair", 80.0)
    assert cart.shipping() == 0.0


def test_empty_cart_has_no_shipping():
    assert Cart().shipping() == 0.0


def test_total_applies_discount():
    cart = Cart()
    cart.add("chair", 100.0)
    assert cart.total(discount_percent=10) == 90.0


def test_total_rejects_invalid_discount():
    cart = Cart()
    with pytest.raises(ValueError):
        cart.total(discount_percent=150)


def test_bulk_discount_tiers():
    assert bulk_discount_percent(1) == 0.0
    assert bulk_discount_percent(20) == 5.0
    assert bulk_discount_percent(100) == 10.0


def test_known_coupon():
    assert coupon_discount("SAVE10") == 10.0
    assert coupon_discount("unknown") == 0.0
