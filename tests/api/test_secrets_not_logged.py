"""
API keys and tokens must never reach api.log or the reports: they are CI artifacts, the Allure report is public,
and against staging the keys are real secrets.
"""
from pathlib import Path

import pytest

import config
from utils.redact import redact, redact_headers

pytestmark = [pytest.mark.api, pytest.mark.auth]


@pytest.mark.parametrize("body, expected", [
    ('{"apiKey": "user-api-key"}', '{"apiKey": "***"}'),
    ('{"role":"user","token":"278f956c-b5e3"}', '{"role":"user","token":"***"}'),
    ('{"apiKey": ["user-api-key"]}', '{"apiKey": "***"}'),                   # a list, from the negative tests
    ('{"apiKey": 12345}', '{"apiKey": "***"}'),                              # a number
    ('{"apiKey": "a\\"b"}', '{"apiKey": "***"}'),                            # an escaped quote inside the value
    ('{"APIKEY": "x", "password": "y"}', '{"APIKEY": "***", "password": "***"}'),
    ('{"api_key": "x", "refresh_token": "y"}', '{"api_key": "***", "refresh_token": "***"}'),   # other spellings
    (f'"{config.env.api_key}"', '"***"'),                      # a bare key as the whole body: a known secret
    (f"{{\"key\": \"{config.env.admin_api_key}\"}}", '{"key": "***"}'),   # a known secret under any field
    ('{"amount": 1e309, "apiKey": "k"', '{"amount": 1e309, "apiKey": "***"'),  # broken JSON, kept as sent
    ("Bearer 0b1c2d3e", "Bearer ***"),
    ('{"message":"Missing bearer token"}', '{"message":"Missing bearer token"}'),   # an error message, not a token
    ('{"name": "Ann Lee", "email": "ann@qa.test"}', '{"name": "Ann Lee", "email": "ann@qa.test"}'),  # no secrets
    (b'{"apiKey": "user-api-key"}', '{"apiKey": "***"}'),                     # bytes, as requests stores them
    (None, ""),
], ids=["api-key", "token", "list", "number", "escaped-quote", "any-case", "other-spellings", "bare-known-key",
        "known-key-any-field", "broken-json", "bearer", "bearer-in-a-message", "nothing-secret", "bytes", "no-body"])
def test_redact_masks_credentials(body, expected):
    assert redact(body) == expected


def test_redact_headers_masks_authorization():
    headers = redact_headers({"Authorization": "Bearer abc", "Content-Type": "application/json"})
    assert headers == {"Authorization": "***", "Content-Type": "application/json"}


def test_login_leaves_no_secret_in_api_log(user_api, request):
    """End to end: user_api logged in through the real Client, so its key and token went through _log."""
    token = user_api.session.headers["Authorization"].removeprefix("Bearer ")
    user_api.get("/api/users/123")                                     # one more call, sent with the token

    log_file = Path(request.config.option.log_file or request.config.getini("log_file"))   # per worker with -n
    text = (log_file if log_file.is_absolute() else request.config.rootpath / log_file).read_text()

    assert "/api/auth/token -> 200" in text, "the login wasn't logged, so this test checked nothing"
    assert config.env.api_key not in text, "an API key was written to api.log"
    assert token not in text, "a bearer token was written to api.log"
