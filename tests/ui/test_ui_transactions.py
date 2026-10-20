import pytest

from tests.ui.pages import FinPayPage
from playwright.sync_api import Page, expect
from utils.factories import make_transaction

pytestmark = pytest.mark.ui


def test_transfer(page: Page):
    tx = make_transaction(amount=100.50)
    app = FinPayPage(page)
    app.open_and_sign_in()
    app.send_transaction(tx)
      
    expect(page.get_by_test_id("tx-result")).to_have_text("Transaction completed: transfer of $100.50")

@pytest.mark.negative
def test_transfer_with_negative_amount_shows_error(page: Page):
    app = FinPayPage(page)
    app.open_and_sign_in()
    app.send_transaction(make_transaction(amount=-5))
      
    expect(page.get_by_test_id("error-amount")).to_have_text("amount must be greater than 0")
