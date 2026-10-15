# shopcart

A tiny shopping-cart library used as the **target repository** for System Factory demos.
It contains intentionally planted bugs (documented in System Factory's DEMO.md, not here).

## Installation

No dependencies beyond Python 3.11+ and pytest. Run the tests with `python -m pytest -q`.

## Creating a cart

```python
from shopcart.cart import Cart
cart = Cart()
cart.add("mug", 12.50, quantity=2)
cart.subtotal()   # 25.0
```

## Shipping

Orders of $50.00 or more ship free. Smaller orders pay a flat $4.99 shipping fee.

## Applying a coupon code

Look up the coupon's discount percent with `coupon_discount` and pass it to `total` at checkout:

```python
from shopcart.cart import Cart, coupon_discount
cart.total(discount_percent=coupon_discount("SAVE10"))
```

Known coupon codes are `SAVE10` (10% off) and `HALF` (50% off). Unknown codes give no discount.

## Bulk discounts

`bulk_discount_percent(quantity)` gives 5% off for 10 or more units and 10% off for 50 or more units.

## Pagination

`paginate(items, page, page_size)` returns the items on a 1-indexed page and
`page_count(items, page_size)` returns how many pages there are.
