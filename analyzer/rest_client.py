"""HTTP wrapper around Tool Cohort API."""
import requests


class RestClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_rule(self, intent: str) -> dict:
        r = requests.get(f"{self.base_url}/rules/{intent}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def call_tool(self, endpoint: str, payload: dict) -> dict:
        r = requests.post(f"{self.base_url}{endpoint}", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()