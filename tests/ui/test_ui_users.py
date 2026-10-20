import pytest

from conftest import Client
from tests.ui.pages import FinPayPage
from playwright.sync_api import Page, expect

from utils.factories import make_user

pytestmark = pytest.mark.ui


def test_register_user(page: Page, user_api: Client):
    user = make_user()
    app = FinPayPage(page)
    app.open_and_sign_in()
    app.register(user)

    expect(page.get_by_test_id("register-result")).to_contain_text(f"User created: {user['name']}")
    user_id = page.get_by_test_id("created-user-id").inner_text()

    saved = user_api.get(f"/api/users/{user_id}").json()
    assert saved["email"] == user["email"]
    assert saved["accountType"] == user["accountType"]

@pytest.mark.negative
def test_register_user_with_invalid_email(page: Page):
    app = FinPayPage(page)
    app.open_and_sign_in()
    app.register(make_user(email="invalid_email"))
    expect(page.get_by_test_id("register-result")).to_contain_text("email must be a valid email address")