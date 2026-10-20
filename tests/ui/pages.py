import config
from playwright.sync_api import expect

UI_URL = config.env.base_url + "/ui"

class FinPayPage:
    def __init__(self, page):
        self.page = page

    def open_and_sign_in(self, api_key=config.env.api_key):
        """Signs in as the configured normal user unless another key is given."""
        self.page.goto(UI_URL)
        self.page.get_by_test_id("api-key").fill(api_key)
        self.page.get_by_test_id("login-submit").click()
        expect(self.page.get_by_test_id("login-status")).to_contain_text("Signed in")

    def register(self, user):
        self.page.get_by_test_id("register-name").fill(user["name"])
        self.page.get_by_test_id("register-email").fill(user["email"])
        self.page.get_by_test_id("register-account-type").select_option(user["accountType"])
        self.page.get_by_test_id("register-submit").click()

    def send_transaction(self, tx):
        self.page.get_by_test_id("tx-user-id").fill(tx["userId"])
        self.page.get_by_test_id("tx-type").select_option(tx["type"])
        self.page.get_by_test_id("tx-amount").fill(str(tx["amount"]))
        if tx["type"] == "transfer":
            self.page.get_by_test_id("tx-recipient-id").fill(tx["recipientId"])
        self.page.get_by_test_id("tx-submit").click()

