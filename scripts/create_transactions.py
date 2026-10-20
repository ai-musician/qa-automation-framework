"""
Create the transactions listed in a JSON file by sending them to the running server.
    python -m mock_server.app                  # terminal 1: start the server
    python scripts/create_transactions.py      # terminal 2: uses data/transactions.json
    python scripts/create_transactions.py my.json  # or another file
"""
import json
import os
import sys
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:5050")
API_KEY = os.getenv("API_KEY", "user-api-key")
file_path = sys.argv[1] if len(sys.argv) > 1 else  "data/transactions.json" 

with open(file_path) as f:
    transactions = json.load(f) # a list of dicts

# step 1: trade the API key for a token
token = requests.post(f"{BASE_URL}/api/auth/token", json={"apiKey": API_KEY}).json()["token"]

for transaction in transactions:
    response = requests.post(f"{BASE_URL}/api/transactions", json=transaction, headers={"Authorization": f"Bearer {token}"})
    print(response.status_code, response.json())