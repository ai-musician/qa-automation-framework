"""Test settings. """
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

@dataclass(frozen=True)
class Env:
    name: str
    base_url: str          # UI
    api_base_url: str      # API gateway
    api_key: str           # regular user
    admin_api_key: str     # admin role
    start_mock_server: bool
    perf_load: float       # multiplies the request counts in tests/perf (CI: 1 on pull requests, 3 on main)

ENV_NAME = os.getenv("ENV_NAME", "local")
load_dotenv(Path(__file__).parent / f".env.{ENV_NAME}")   # e.g. .env.staging; ignored if missing


env = Env(
    name=os.getenv("ENV_NAME", "local"),
    base_url=os.getenv("BASE_URL", "http://localhost:5050"),
    api_base_url=os.getenv("API_BASE_URL", "http://localhost:5050"),
    api_key=os.getenv("API_KEY", "user-api-key"),
    admin_api_key=os.getenv("ADMIN_API_KEY", "admin-api-key"),
    start_mock_server=os.getenv("START_MOCK_SERVER", "true").lower() == "true",
    perf_load=float(os.getenv("PERF_LOAD", "1")),
)