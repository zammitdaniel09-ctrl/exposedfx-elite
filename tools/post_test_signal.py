# tools/post_test_signal.py
# Posts one test signal to the local server.

import os
import json
import requests
from pathlib import Path

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
AUTO_TOKEN = os.environ.get("AUTO_TOKEN", "change-this-token")

sample_path = Path(__file__).with_name("sample_signal.json")
payload = json.loads(sample_path.read_text(encoding="utf-8"))

r = requests.post(
    f"{SERVER_URL}/api/v1/signals",
    json=payload,
    headers={"X-AUTO-TOKEN": AUTO_TOKEN},
    timeout=10,
)

print("Status:", r.status_code)
print(r.text)
