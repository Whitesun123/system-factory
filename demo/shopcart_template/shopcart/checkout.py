"""Shipping rules for checkout.

Orders of $50.00 or more ship free. Smaller orders pay a flat $4.99 fee, and the checkout banner tells
the customer how much more they need to add to qualify for free shipping.
"""

FREE_SHIPPING_THRESHOLD = 50.0
FLAT_FEE = 4.99


def shipping_cost(subtotal: float) -> float:
    """Shipping charged for an order subtotal.

    >>> shipping_cost(20.0)
    4.99
    >>> shipping_cost(80.0)
    0.0
    """
    if subtotal > FREE_SHIPPING_THRESHOLD:
        return 0.0
    return FLAT_FEE


def shipping_banner(subtotal: float) -> str:
    """Banner shown at checkout.

    >>> shipping_banner(80.0)
    'Your order ships free!'
    >>> shipping_banner(45.0)
    'Add $5.00 more for free shipping'
    """
    remaining = FREE_SHIPPING_THRESHOLD - subtotal
    if remaining < 0:
        return "Your order ships free!"
    return f"Add ${remaining:.2f} more for free shipping"
