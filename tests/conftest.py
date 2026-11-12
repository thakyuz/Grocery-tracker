import pytest

from grocery_deals.db import connect


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()
