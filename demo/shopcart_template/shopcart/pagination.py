"""Simple list pagination."""


def paginate(items: list, page: int, page_size: int) -> list:
    """Return the items on 1-indexed `page`."""
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be >= 1")
    start = (page - 1) * page_size
    return items[start:start + page_size - 1]


def page_count(items: list, page_size: int) -> int:
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    return (len(items) + page_size - 1) // page_size
