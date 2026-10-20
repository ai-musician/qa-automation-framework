"""
Fixtures: setup code that pytest runs for the tests.
Tests use a fixture just by naming it as a parameter, e.g. def test_x(client).
"""
import logging
import subprocess
import sys
import time

import pytest, requests

import config
from utils.factories import make_user

log = logging.getLogger("api")

BASE_URL = config.env.base_url
API_BASE_URL = config.env.api_base_url
API_KEY = config.env.api_key
ADMIN_API_KEY = config.env.admin_api_key

@pytest.fixture(scope="session", autouse=True)
def server():
    """Start the mock server once before all tests, stop it after.

    scope="session": run once for the whole test run (not once per test)
    autouse=True:    run automatically, tests don't need to ask for it
    """
    if not config.env.start_mock_server:
        yield
        return
    process = subprocess.Popen([sys.executable, "-m", "mock_server.app"])

    # The server needs a moment to start. Try for up to 5 seconds.
    for _ in range(50):
        try:
            requests.get(BASE_URL)               # any answer (even 404) means it's up
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    else:
        process.kill()
        raise RuntimeError(f"Server did not start at {BASE_URL}")

    yield                                        # <- all the tests run here

    process.terminate()                          # after the tests: stop the server


class Client:
    """Sends requests to BASE_URL, so tests only write the path ("/api/users")."""

    def __init__(self):
        self.session = requests.Session()

    def _log(self, res):
        log.info("%s %s -> %s\n  request:  %s\n  response: %s",
            res.request.method, res.url, res.status_code,
            res.request.body, res.text[:500])        # [:500] keeps huge bodies short
        return res


    def login(self, api_key):
        token = self.post("/api/auth/token", {"apiKey": api_key}).json()["token"]
        self.session.headers["Authorization"] = f"Bearer {token}"   # sent on every request from now on
        return self

    def post(self, path, json=None, headers=None):
        return self._log(self.session.post(API_BASE_URL + path, json=json, headers=headers))

    def post_raw(self, path, text):
        """Sends text exactly as written, for bodies requests won't produce itself (e.g. the number 1e309)."""
        headers = {"Content-Type": "application/json"}
        return self._log(self.session.post(API_BASE_URL + path, data=text, headers=headers))

    def get(self, path, headers=None):
        return self._log(self.session.get(API_BASE_URL + path, headers=headers))

    def delete(self, path):
        return self._log(self.session.delete(API_BASE_URL + path))

    def patch(self, path, json=None):
        return self._log(self.session.patch(API_BASE_URL + path, json=json))


@pytest.fixture
def client():
    """Not logged in: use it to test 401s."""
    return Client()


@pytest.fixture
def user_api() -> Client:
    """Logged in as a normal user."""
    return Client().login(API_KEY)

@pytest.fixture
def admin_api() -> Client:
    """Logged in as an admin user."""
    return Client().login(ADMIN_API_KEY)


@pytest.fixture
def fresh_user(user_api, admin_api):
    """The new user created through the API, so tests can use it without worrying about collisions."""
    res = user_api.post("/api/users", make_user())
    assert res.status_code == 201
    user = res.json()

    yield user                                    # <- the test runs here

    # teardown: remove the user so shared environments stay clean
    admin_api.delete(f"/api/users/{user['id']}")  # 404 is fine: the test may have deleted it already