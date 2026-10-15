"""Demo target repository setup and sample issues.

The demo repo is generated from demo/shopcart_template into data/repos/shopcart as its own,
separate git repository. System Factory only ever reads its main branch and works in worktrees.
"""
from __future__ import annotations

import shutil

from .config import DEMO_TEMPLATE, Settings
from .sandbox import Git


def init_demo_repo(settings: Settings, *, force: bool = False) -> str:
    """Create the demo repo if missing. Never overwrites an existing repo unless force=True."""
    settings.ensure_dirs()
    repo = settings.demo_repo
    git = Git(settings.empty_gitconfig)
    if (repo / ".git").exists() and not force:
        return f"demo repo already exists at {repo} (kept as-is)"
    if repo.exists():
        if not force:
            raise FileExistsError(f"{repo} exists but is not a git repo; refusing to overwrite")
        if settings.worktrees_dir.exists():
            shutil.rmtree(settings.worktrees_dir)
            settings.worktrees_dir.mkdir()
        shutil.rmtree(repo)
    shutil.copytree(DEMO_TEMPLATE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    # A checkout made with core.autocrlf=true (the Git for Windows default) has CRLF files; the demo repo is
    # always generated with LF so results do not depend on how System Factory itself was cloned.
    for f in repo.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".md", ".toml", ".txt", ".cfg"):
            data = f.read_bytes()
            if b"\r\n" in data:
                f.write_bytes(data.replace(b"\r\n", b"\n"))
    git.run(repo, "init", "-q", "-b", "main")
    git.run(repo, "add", "-A")
    # Fixed dates: the same template always yields the same base commit SHA, on any machine.
    fixed = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}
    git.run(repo, "commit", "-q", "-m", "Initial shopcart demo library (contains planted bugs)", extra_env=fixed)
    return f"demo repo created at {repo} ({git.out(repo, 'rev-parse', '--short', 'HEAD')})"


SAMPLES: dict[str, dict] = {
    "shipping": {
        "label": "Bug: free shipping at exactly $50 (fixed on attempt 1)",
        "title": "Free shipping not applied for orders of exactly $50",
        "body": (
            "Customers whose cart is exactly $50.00 are charged $4.99 shipping, but our policy "
            "(see README) is that orders of $50 or more ship free. This is wrong and customers complain.\n\n"
            "```python\n"
            ">>> from shopcart.cart import Cart\n"
            ">>> cart = Cart()\n"
            ">>> cart.add(\"mug\", 25.0, quantity=2)\n"
            ">>> cart.shipping()\n"
            "0.0\n"
            "```\n"),
    },
    "pagination": {
        "label": "Bug: pagination drops last item (attempt 1 fails, retry succeeds)",
        "title": "Last item missing from every page",
        "body": (
            "Paginating a product list returns one item fewer than the page size; the last item of every "
            "page is missing.\n\n"
            "```python\n"
            ">>> from shopcart.pagination import paginate\n"
            ">>> paginate(list(range(10)), page=1, page_size=3)\n"
            "[0, 1, 2]\n"
            "```\n"),
    },
    "bulk": {
        "label": "Bug: bulk discount at exactly 10 units (fails review in iteration 1, corrected in iteration 2)",
        "title": "Bulk discount not applied when ordering exactly 10 units",
        "body": (
            "The README and docstring promise 5% off for 10 or more units, but an order of exactly 10 units "
            "gets no discount. This is wrong.\n\n"
            "```python\n"
            ">>> from shopcart.cart import bulk_discount_percent\n"
            ">>> bulk_discount_percent(10)\n"
            "5.0\n"
            "```\n"),
    },
    "checkout": {
        "label": "Bug: $50 order charged shipping (real-LLM demo; a related defect is not mentioned in the report)",
        "title": "Customers with exactly $50.00 are charged shipping",
        "body": (
            "Our policy is that orders of $50.00 or more ship free, but a customer whose order came to exactly "
            "$50.00 was charged the $4.99 fee at checkout. This is wrong.\n\n"
            "```python\n"
            ">>> from shopcart.checkout import shipping_cost\n"
            ">>> shipping_cost(50.0)\n"
            "0.0\n"
            "```\n"),
    },
    "coupon": {
        "label": "Bug: lowercase coupon codes (beyond simulated agent; exhausts retries)",
        "title": "Coupon codes rejected when typed in lowercase",
        "body": (
            "Customers typing `save10` instead of `SAVE10` get no discount. Coupon codes should be "
            "case-insensitive; this is incorrect behaviour.\n\n"
            "```python\n"
            ">>> from shopcart.cart import coupon_discount\n"
            ">>> coupon_discount(\"save10\")\n"
            "10.0\n"
            "```\n"),
    },
    "not_reproducible": {
        "label": "Bug report that cannot be reproduced (blocks for more info)",
        "title": "Cart subtotal wrong for multiple items",
        "body": (
            "I think the subtotal is incorrect when a line item has a quantity above one.\n\n"
            "```python\n"
            ">>> from shopcart.cart import Cart\n"
            ">>> cart = Cart()\n"
            ">>> cart.add(\"pen\", 1.5, quantity=4)\n"
            ">>> cart.subtotal()\n"
            "6.0\n"
            "```\n"),
    },
    "feature": {
        "label": "Feature request: CSV export (blocks: needs LLM/human developer)",
        "title": "Add CSV export for cart contents",
        "body": "As a store admin I would like to export the cart contents as CSV (sku, unit price, "
                "quantity, line subtotal) so I can import orders into our accounting tool.",
    },
    "support": {
        "label": "Support question: how to apply a coupon (answer drafted from docs)",
        "title": "How do I apply a coupon code at checkout?",
        "body": "I have a coupon code from a newsletter. How do I use it with the cart total?",
    },
}
