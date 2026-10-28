"""
Fixtures: setup code that pytest runs for the tests.
Tests use a fixture just by naming it as a parameter, e.g. def test_x(client).
"""
import logging
import shutil
import subprocess
import sys
import time

import allure
import pytest, requests

import config as settings   # "config" is also the name pytest gives its own config object in hooks
from utils.factories import make_user
from utils.redact import add_secret, redact, redact_headers

log = logging.getLogger("api")

BASE_URL = settings.env.base_url
API_BASE_URL = settings.env.api_base_url
API_KEY = settings.env.api_key
ADMIN_API_KEY = settings.env.admin_api_key
add_secret(API_KEY)                  # masked in api.log and the reports, wherever they appear (utils/redact.py)
add_secret(ADMIN_API_KEY)

# ---------- mock server: started once, also when tests run in parallel ----------
#
# With pytest-xdist (pytest -n 4) there is one main pytest process and 4 worker processes that run the tests.
# A session fixture would run once in EACH worker: 4 servers fighting over port 5050. These hooks run in the main
# process only, before any worker starts, so all workers share one server.
#
#   pytest_configure:    runs once per process when pytest starts (main process, and each worker)
#   pytest_unconfigure:  runs once per process when pytest exits
#   config.workerinput:  only exists in a worker, so "not a worker" = the main process (or a normal run)

def is_worker(config) -> bool:
    return hasattr(config, "workerinput")


@pytest.hookimpl(tryfirst=True)      # before allure-pytest creates its results folder
def pytest_configure(config):
    if is_worker(config):
        # Each worker logs to its own file (reports/api-gw0.log, api-gw1.log, ...): if they all wrote to
        # reports/api.log they would overwrite each other. utils/kpi.py reads all of them.
        worker = config.workerinput["workerid"]          # "gw0", "gw1", ...
        config.option.log_file = str(config.rootpath / "reports" / f"api-{worker}.log")
        return

    for old_log in (config.rootpath / "reports").glob("api-gw*.log"):     # from an earlier parallel run
        old_log.unlink()
    # Allure results from the last run, cleared here once. allure-pytest's own --clean-alluredir runs in every
    # worker, and a worker starting late can delete the folder another worker just created.
    if config.option.allure_report_dir:
        shutil.rmtree(config.rootpath / config.option.allure_report_dir, ignore_errors=True)

    if not settings.env.start_mock_server:
        return
    config.mock_server = start_mock_server()


def pytest_unconfigure(config):
    if hasattr(config, "mock_server"):
        config.mock_server.terminate()                   # after the tests: stop the server


def start_mock_server() -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, "-m", "mock_server.app"])

    # The server needs a moment to start. Try for up to 5 seconds.
    for _ in range(50):
        try:
            requests.get(BASE_URL)               # any answer (even 404) means it's up
            return process
        except requests.ConnectionError:
            time.sleep(0.1)
    process.kill()
    raise RuntimeError(f"Server did not start at {BASE_URL}")


# ---------- Allure labels ----------
LAYERS = {"api": "API", "ui": "UI", "perf": "Performance"}      # pytest marker -> Allure epic


@pytest.fixture(autouse=True)
def allure_labels(request):
    """Groups the Allure report by layer (API / UI / Performance) and by test module (users, transactions...)."""
    epic = next((name for marker, name in LAYERS.items() if request.node.get_closest_marker(marker)), "Other")
    allure.dynamic.epic(epic)
    module = request.node.module.__name__.rsplit(".", 1)[-1]       # tests.api.test_users -> test_users
    allure.dynamic.feature(module.removeprefix("test_").removeprefix("ui_").replace("_", " "))

    # Allure records each parametrized test's inputs, and negative cases carry API keys: mask them too.
    # Allure also identifies a test across runs by its parameter values, and masked values can be identical
    # ([api-key] and [list] both become {"apiKey": "***"}), which would merge two cases into one. So the
    # masked value is shown but excluded from that identity, and the pytest case id identifies the test instead.
    params = request.node.callspec.params if hasattr(request.node, "callspec") else {}
    shown = {name: value if isinstance(value, str) else repr(value) for name, value in params.items()}
    masked = {name: m for name, text in shown.items() if (m := redact(text)) != text}
    if masked:
        allure.dynamic.parameter("pytest id", request.node.callspec.id)      # "case" is taken by some tests
        for name, value in masked.items():
            allure.dynamic.parameter(name, value, excluded=True)


class Client:
    """Sends requests to BASE_URL, so tests only write the path ("/api/users")."""

    def __init__(self):
        self.session = requests.Session()

    def _log(self, res):
        """Every request and response goes to api.log and, as an attachment, to the test in the Allure report,
        with API keys and tokens masked (utils/redact.py): both end up in CI artifacts and on GitHub Pages."""
        request_body, response_body = redact(res.request.body), redact(res.text[:500])   # [:500]: huge bodies
        log.info("%s %s -> %s\n  request:  %s\n  response: %s",
            res.request.method, res.url, res.status_code, request_body, response_body)
        path = res.request.path_url
        allure.attach(f"{res.request.method} {res.url} -> {res.status_code}\n\n"
                      f"Request headers:\n{redact_headers(res.request.headers)}\n\n"
                      f"Request body:\n{request_body}\n\nResponse body:\n{response_body}",
                      name=f"{res.request.method} {path} -> {res.status_code}",
                      attachment_type=allure.attachment_type.TEXT)
        return res


    def login(self, api_key):
        token = self.post("/api/auth/token", {"apiKey": api_key}).json()["token"]
        add_secret(token)
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