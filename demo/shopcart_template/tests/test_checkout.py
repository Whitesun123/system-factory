from shopcart.checkout import shipping_banner, shipping_cost


def test_small_order_pays_flat_fee():
    assert shipping_cost(10.0) == 4.99


def test_large_order_ships_free():
    assert shipping_cost(120.0) == 0.0


def test_banner_for_large_order():
    assert shipping_banner(99.0) == "Your order ships free!"


def test_banner_for_small_order():
    assert shipping_banner(30.0) == "Add $20.00 more for free shipping"
