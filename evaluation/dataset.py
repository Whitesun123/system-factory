"""Reviewer evaluation dataset: issues and patches only. Labels and oracles live in evaluation/labels.json and are never
shown to reviewers. Each case is a tiny repository: `files` (the code before the patch, plus its existing tests), an
issue with a doctest example of the reported symptom, and `patch` (the changed file after the patch).

Every defective patch fixes the *reported* symptom (the reproduction test passes and the existing tests pass); the
defect is elsewhere in the specification. Correct patches satisfy the whole specification.
"""

CHECKOUT = '''"""Shipping rules for checkout.

Orders of $50.00 or more ship free. Smaller orders pay a flat $4.99 fee, and the checkout banner tells
the customer how much more they need to add to qualify for free shipping.
"""

FREE_SHIPPING_THRESHOLD = 50.0
FLAT_FEE = 4.99


def shipping_cost(subtotal: float) -> float:
    """Shipping charged for an order subtotal.

    >>> shipping_cost(20.0)
    4.99
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
'''
CHECKOUT_TESTS = '''from shopkit.checkout import shipping_banner, shipping_cost


def test_flat_fee():
    assert shipping_cost(10.0) == 4.99


def test_large_order_ships_free():
    assert shipping_cost(120.0) == 0.0


def test_banner_small_order():
    assert shipping_banner(30.0) == "Add $20.00 more for free shipping"
'''

BULK = '''def bulk_discount_percent(quantity: int) -> float:
    """Volume discount: 5% off for 10 or more units, 10% off for 50 or more units."""
    if quantity > 50:
        return 10.0
    if quantity > 10:
        return 5.0
    return 0.0
'''
BULK_TESTS = '''from shopkit.bulk import bulk_discount_percent


def test_tiers():
    assert bulk_discount_percent(1) == 0.0
    assert bulk_discount_percent(20) == 5.0
    assert bulk_discount_percent(100) == 10.0
'''

PAGINATION = '''def paginate(items: list, page: int, page_size: int) -> list:
    """Return the items on 1-indexed `page`; pages beyond the end are empty."""
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be >= 1")
    start = (page - 1) * page_size
    return items[start:start + page_size - 1]
'''
PAGINATION_TESTS = '''import pytest

from shopkit.pagination import paginate


def test_rejects_page_zero():
    with pytest.raises(ValueError):
        paginate([1, 2, 3], 0, 2)


def test_beyond_end():
    assert paginate([1, 2, 3], 5, 2) == []
'''

ACCESS = '''def can_checkout(cart_items: int, account_locked: bool) -> bool:
    """A customer can check out only with at least one item in the cart and an unlocked account."""
    return cart_items > 0 or not account_locked
'''
ACCESS_TESTS = '''from shopkit.access import can_checkout


def test_normal_checkout():
    assert can_checkout(2, False) is True
'''

CLAMP = '''def clamp_discount(percent: float) -> float:
    """Clamp a discount percentage into the valid range 0 to 100 (inclusive)."""
    return percent
'''
CLAMP_TESTS = '''from shopkit.discounts import clamp_discount


def test_valid_value_unchanged():
    assert clamp_discount(25.0) == 25.0
'''

QUANTITY = '''def parse_quantity(text: str) -> int:
    """Parse a quantity typed by a customer, such as '3' or ' 3 '. Empty text means a quantity of 1."""
    text = text.strip()
    if not text:
        return 1
    return int(text)
'''
QUANTITY_TESTS = '''from shopkit.quantity import parse_quantity


def test_plain_number():
    assert parse_quantity("4") == 4
    assert parse_quantity(" 7 ") == 7
'''

PRICING = '''def unit_price(total: float, quantity: int) -> float:
    """Price per unit. Raises ValueError if quantity is not a positive number."""
    return total / quantity
'''
PRICING_TESTS = '''from shopkit.pricing import unit_price


def test_unit_price():
    assert unit_price(10.0, 4) == 2.5
'''

SKU = '''def valid_sku(sku: str) -> bool:
    """A SKU is 3 to 12 characters long and contains only uppercase letters and digits."""
    return 3 <= len(sku) <= 12
'''
SKU_TESTS = '''from shopkit.sku import valid_sku


def test_lengths():
    assert valid_sku("AB") is False
    assert valid_sku("ABCDEFGHIJKLM") is False
    assert valid_sku("ABC123") is True
'''

TEMPLATES = '''import os

TEMPLATE_DIR = "templates"


def template_path(name: str) -> str:
    """Path of a named email template inside TEMPLATE_DIR.

    Template names must not contain path separators ('/' or '\\\\') or '..'; such names raise ValueError.
    """
    return os.path.join(TEMPLATE_DIR, name + ".txt")
'''
TEMPLATES_TESTS = '''import os

from shopkit.templates import template_path


def test_simple_name():
    assert template_path("welcome") == os.path.join("templates", "welcome.txt")
'''

LOYALTY = '''def loyalty_tier(points: int) -> str:
    """Customers reach 'gold' at 1000 points and 'silver' at 500 points; everyone else is 'bronze'."""
    if points > 1000:
        return "gold"
    if points > 500:
        return "silver"
    return "bronze"
'''
LOYALTY_TESTS = '''from shopkit.loyalty import loyalty_tier


def test_tiers():
    assert loyalty_tier(10) == "bronze"
    assert loyalty_tier(700) == "silver"
    assert loyalty_tier(5000) == "gold"
'''

TAX = '''"""Sales tax. The sales tax rate is 8%."""


def tax(amount: float) -> float:
    """Sales tax on an amount, rounded to cents."""
    return round(amount * 0.07, 2)


def total_with_tax(amount: float) -> float:
    """The amount plus sales tax, rounded to cents."""
    return round(amount * 1.07, 2)
'''
TAX_TESTS = '''from shopkit.tax import tax, total_with_tax


def test_zero():
    assert tax(0.0) == 0.0
    assert total_with_tax(0.0) == 0.0
'''

RETURNS = '''RETURN_DAYS = 30


def can_return(days_since_purchase: int) -> bool:
    """Returns are accepted up to and including 30 days after purchase."""
    return days_since_purchase < RETURN_DAYS


def return_policy_message() -> str:
    """Message printed on receipts: 'Returns accepted for 30 days'."""
    return f"Returns accepted for {RETURN_DAYS} days"
'''
RETURNS_TESTS = '''from shopkit.returns import can_return, return_policy_message


def test_early_return():
    assert can_return(3) is True
    assert can_return(90) is False


def test_message_mentions_days():
    assert "days" in return_policy_message()
'''


def _issue(title: str, body: str, example: str) -> dict:
    return {"title": title, "body": f"{body}\n\n```python\n{example.strip()}\n```\n"}


ISSUES = {
    "checkout": _issue("Customers with exactly $50.00 are charged shipping",
                       "Our policy is that orders of $50.00 or more ship free, but an order of exactly $50.00 was "
                       "charged the $4.99 fee. This is wrong.",
                       ">>> from shopkit.checkout import shipping_cost\n>>> shipping_cost(50.0)\n0.0"),
    "bulk": _issue("Bulk discount not applied when ordering exactly 10 units",
                   "An order of exactly 10 units gets no discount, which is wrong.",
                   ">>> from shopkit.bulk import bulk_discount_percent\n>>> bulk_discount_percent(10)\n5.0"),
    "pagination": _issue("Last item missing from every page", "Each page is missing its last item.",
                         ">>> from shopkit.pagination import paginate\n>>> paginate(list(range(10)), 1, 3)\n[0, 1, 2]"),
    "access": _issue("Locked accounts can still check out", "A locked account was able to check out. This is wrong.",
                     ">>> from shopkit.access import can_checkout\n>>> can_checkout(3, True)\nFalse"),
    "clamp": _issue("A 150% discount makes totals negative", "clamp_discount does not cap the discount.",
                    ">>> from shopkit.discounts import clamp_discount\n>>> clamp_discount(150.0)\n100.0"),
    "quantity": _issue("Quantity '3 units' crashes the cart", "Typing '3 units' raises ValueError; it should mean 3.",
                       ">>> from shopkit.quantity import parse_quantity\n>>> parse_quantity('3 units')\n3"),
    "pricing": _issue("unit_price crashes with ZeroDivisionError for quantity 0",
                      "unit_price(10, 0) raises ZeroDivisionError instead of the documented ValueError.",
                      ">>> from shopkit.pricing import unit_price\n>>> unit_price(10.0, 0)\n"
                      "Traceback (most recent call last):\n...\nValueError: quantity must be positive"),
    "sku": _issue("Lowercase SKUs are accepted", "valid_sku('abc') returns True, which is wrong.",
                  ">>> from shopkit.sku import valid_sku\n>>> valid_sku('abc')\nFalse"),
    "templates": _issue("template_path allows escaping the template directory",
                        "template_path('../secrets') points outside the template directory.",
                        ">>> from shopkit.templates import template_path\n>>> template_path('../secrets')\n"
                        "Traceback (most recent call last):\n...\nValueError: invalid template name"),
    "loyalty": _issue("Customers with exactly 1000 points are not gold",
                      "A customer with exactly 1000 points is shown as silver.",
                      ">>> from shopkit.loyalty import loyalty_tier\n>>> loyalty_tier(1000)\n'gold'"),
    "tax": _issue("Sales tax is calculated at 7% instead of 8%", "tax(100) returns 7.0 but our rate is 8%.",
                  ">>> from shopkit.tax import tax\n>>> tax(100.0)\n8.0"),
    "returns": _issue("Returns are refused on day 30", "A return on day 30 was refused, but the policy allows 30 days.",
                      ">>> from shopkit.returns import can_return\n>>> can_return(30)\nTrue"),
}

BASES = {
    "checkout": ("shopkit/checkout.py", CHECKOUT, CHECKOUT_TESTS), "bulk": ("shopkit/bulk.py", BULK, BULK_TESTS),
    "pagination": ("shopkit/pagination.py", PAGINATION, PAGINATION_TESTS),
    "access": ("shopkit/access.py", ACCESS, ACCESS_TESTS), "clamp": ("shopkit/discounts.py", CLAMP, CLAMP_TESTS),
    "quantity": ("shopkit/quantity.py", QUANTITY, QUANTITY_TESTS), "pricing": ("shopkit/pricing.py", PRICING, PRICING_TESTS),
    "sku": ("shopkit/sku.py", SKU, SKU_TESTS), "templates": ("shopkit/templates.py", TEMPLATES, TEMPLATES_TESTS),
    "loyalty": ("shopkit/loyalty.py", LOYALTY, LOYALTY_TESTS), "tax": ("shopkit/tax.py", TAX, TAX_TESTS),
    "returns": ("shopkit/returns.py", RETURNS, RETURNS_TESTS),
}

# Patches: (case id, base, list of (old, new) replacements applied to the base module).
PATCHES = [
    ("checkout-partial", "checkout", [("if subtotal > FREE_SHIPPING_THRESHOLD:", "if subtotal >= FREE_SHIPPING_THRESHOLD:")]),
    ("checkout-complete", "checkout", [("if subtotal > FREE_SHIPPING_THRESHOLD:", "if subtotal >= FREE_SHIPPING_THRESHOLD:"),
                                       ("if remaining < 0:", "if remaining <= 0:")]),
    ("bulk-partial", "bulk", [("if quantity > 10:", "if quantity >= 10:")]),
    ("bulk-complete", "bulk", [("if quantity > 50:", "if quantity >= 50:"), ("if quantity > 10:", "if quantity >= 10:")]),
    ("pagination-correct", "pagination", [("items[start:start + page_size - 1]", "items[start:start + page_size]")]),
    ("access-wrong-logic", "access", [("    return cart_items > 0 or not account_locked",
                                       "    if account_locked:\n        return False\n    return True")]),
    ("access-correct", "access", [("return cart_items > 0 or not account_locked", "return cart_items > 0 and not account_locked")]),
    ("clamp-missing-lower-bound", "clamp", [("    return percent", "    return min(percent, 100.0)")]),
    ("clamp-correct", "clamp", [("    return percent", "    return max(0.0, min(percent, 100.0))")]),
    ("quantity-regression", "quantity", [("    text = text.strip()\n    if not text:\n        return 1\n    return int(text)",
                                          "    return int(text.strip().split()[0])")]),
    ("quantity-correct", "quantity", [("    return int(text)", "    return int(text.split()[0])")]),
    ("pricing-negative-allowed", "pricing", [("    return total / quantity",
                                              "    try:\n        return total / quantity\n    except ZeroDivisionError:\n"
                                              "        raise ValueError(\"quantity must be positive\")")]),
    ("pricing-correct", "pricing", [("    return total / quantity",
                                     "    if quantity <= 0:\n        raise ValueError(\"quantity must be positive\")\n"
                                     "    return total / quantity")]),
    ("sku-isupper-only", "sku", [("    return 3 <= len(sku) <= 12", "    return 3 <= len(sku) <= 12 and sku.isupper()")]),
    ("sku-correct", "sku", [("    return 3 <= len(sku) <= 12",
                             "    return 3 <= len(sku) <= 12 and all(c.isdigit() or ('A' <= c <= 'Z') for c in sku)")]),
    ("templates-prefix-check-only", "templates", [("    return os.path.join(TEMPLATE_DIR, name + \".txt\")",
                                                   "    if name.startswith(\"..\"):\n        raise ValueError(\"invalid template name\")\n"
                                                   "    return os.path.join(TEMPLATE_DIR, name + \".txt\")")]),
    ("templates-correct", "templates", [("    return os.path.join(TEMPLATE_DIR, name + \".txt\")",
                                         "    if \"/\" in name or \"\\\\\" in name or \"..\" in name:\n"
                                         "        raise ValueError(\"invalid template name\")\n"
                                         "    return os.path.join(TEMPLATE_DIR, name + \".txt\")")]),
    ("loyalty-partial", "loyalty", [("if points > 1000:", "if points >= 1000:")]),
    ("loyalty-correct", "loyalty", [("if points > 1000:", "if points >= 1000:"), ("if points > 500:", "if points >= 500:")]),
    ("tax-partial", "tax", [("return round(amount * 0.07, 2)", "return round(amount * 0.08, 2)")]),
    ("tax-correct", "tax", [("return round(amount * 0.07, 2)", "return round(amount * 0.08, 2)"),
                            ("return round(amount * 1.07, 2)", "return round(amount * 1.08, 2)")]),
    ("returns-constant-changed", "returns", [("RETURN_DAYS = 30", "RETURN_DAYS = 31")]),
    ("returns-correct", "returns", [("return days_since_purchase < RETURN_DAYS", "return days_since_purchase <= RETURN_DAYS")]),
]


def build_case(case_id: str) -> dict:
    """Materialise a case: {id, issue, module path, before, after, tests} (no labels)."""
    cid, base, edits = next(p for p in PATCHES if p[0] == case_id)
    path, before, tests = BASES[base]
    after = before
    for old, new in edits:
        if after.count(old) != 1:
            raise ValueError(f"{case_id}: patch anchor not unique: {old!r}")
        after = after.replace(old, new, 1)
    return {"id": cid, "base": base, "issue": ISSUES[base], "path": path, "before": before, "after": after,
            "tests": tests, "tests_path": f"tests/test_{base}.py"}


CASE_IDS = [p[0] for p in PATCHES]
