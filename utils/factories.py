"""Test data factories: valid data by default, override any field for negative tests."""

from faker import Faker

fake = Faker()

# Seeded users in the mock gateway (ids from the assessment's example payloads)
SENDER_ID = "123"
RECIPIENT_ID = "456"

def make_user(**overrides) -> dict:
    user = {
        "name": fake.name(),
        "email": fake.unique.email(),
        "accountType": fake.random_element(["standard", "premium"]),
    }
    return user | overrides

def make_deposit(**overrides) -> dict:
    return {
        "userId": SENDER_ID if overrides.get("userId") is None else overrides.get("userId"),
        "amount": float(fake.pydecimal(left_digits=6, right_digits=2, positive=True)),   # under the 1,000,000 limit
        "type": "deposit"
    }

def make_transaction(**overrides) -> dict:
    """Create fresh test data every call to avoid collision tests"""
    tx = {
        "userId": SENDER_ID if overrides.get("userId") is None else overrides.get("userId"),
        "amount": float(fake.pydecimal(left_digits=6, right_digits=2, positive=True)),   # under the 1,000,000 limit
        "type": "transfer",
        "recipientId": RECIPIENT_ID if overrides.get("recipientId") is None else overrides.get("recipientId"),
    }
    return tx | overrides
