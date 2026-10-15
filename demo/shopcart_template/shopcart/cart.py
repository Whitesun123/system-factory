"""Shopping cart with coupons and shipping."""
from dataclasses import dataclass, field

FREE_SHIPPING_THRESHOLD = 50.0
FLAT_SHIPPING = 4.99
COUPONS = {"SAVE10": 10.0, "HALF": 50.0}


@dataclass
class LineItem:
    sku: str
    unit_price: float
    quantity: int = 1

    def subtotal(self) -> float:
        return self.unit_price * self.quantity


@dataclass
class Cart:
    items: list = field(default_factory=list)

    def add(self, sku: str, unit_price: float, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if unit_price < 0:
            raise ValueError("unit_price must not be negative")
        self.items.append(LineItem(sku, unit_price, quantity))

    def subtotal(self) -> float:
        return round(sum(item.subtotal() for item in self.items), 2)

    def shipping(self) -> float:
        if not self.items:
            return 0.0
        if self.subtotal() > FREE_SHIPPING_THRESHOLD:
            return 0.0
        return FLAT_SHIPPING

    def total(self, discount_percent: float = 0.0) -> float:
        if not 0 <= discount_percent <= 100:
            raise ValueError("discount_percent must be between 0 and 100")
        discounted = self.subtotal() * (1 - discount_percent / 100)
        return round(discounted + self.shipping(), 2)


def bulk_discount_percent(quantity: int) -> float:
    """Volume discount: 5% off for 10 or more units, 10% off for 50 or more units.

    >>> bulk_discount_percent(9)
    0.0
    >>> bulk_discount_percent(10)
    5.0
    >>> bulk_discount_percent(50)
    10.0
    """
    if quantity > 50:
        return 10.0
    if quantity > 10:
        return 5.0
    return 0.0


def coupon_discount(code: str) -> float:
    """Return the discount percent for a coupon code, or 0.0 if the code is unknown."""
    return COUPONS.get(code.strip(), 0.0)
