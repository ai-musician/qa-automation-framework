"""
Create the users listed in a JSON file by sending them to the running server.

    python -m mock_server.app                  # terminal 1: start the server
    python scripts/create_users.py             # terminal 2: uses data/users.json
    python scripts/create_users.py my.json     # or another file
"""
import json
import os
import sys

import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:5050")
API_KEY = os.getenv("API_KEY", "user-api-key")
file_path = sys.argv[1] if len(sys.argv) > 1 else "data/users.json"

with open(file_path) as f:
    users = json.load(f)                      # a list of dicts

# step 1: trade the API key for a token
token = requests.post(f"{BASE_URL}/api/auth/token", json={"apiKey": API_KEY}).json()["token"]

for user in users:
    response = requests.post(f"{BASE_URL}/api/users", json=user, headers={"Authorization": f"Bearer {token}"})
    print(response.status_code, response.json())
