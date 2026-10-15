import pytest

from shopcart.pagination import page_count, paginate


def test_paginate_rejects_page_zero():
    with pytest.raises(ValueError):
        paginate([1, 2, 3], page=0, page_size=2)


def test_paginate_empty_page_beyond_end():
    assert paginate([1, 2, 3], page=5, page_size=2) == []


def test_page_count():
    assert page_count(list(range(10)), 3) == 4
    assert page_count([], 3) == 0
    assert page_count(list(range(9)), 3) == 3
