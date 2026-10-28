"""
A requests.Session with a default timeout, so a stuck server fails a request in seconds instead of hanging the run
until the CI job's time limit. Used by the API client (conftest.py) and the load tests (utils/load.py).
"""
import requests

TIMEOUT = 10   # seconds: far above every latency SLO, so only a stuck request ever reaches it


class Session(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(*args, **kwargs)
