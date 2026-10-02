#!/usr/bin/env python3
"""Run the live SHACL integration tests against the deployed Ontop app.

Called by ``make shacl-it`` after that target redeploys the ``shacl-it`` instance.
This script uses the Databricks SDK to locate the app and obtain auto-refreshing
user credentials, probes ``/health`` until Ontop is ready, then runs the
integration tests against the app's ``/sparql`` endpoint.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
from databricks.sdk import WorkspaceClient

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "src" / "app"
APP_NAME = "mcp-ontop-vkg-shacl-it"
TIMEOUT_SECONDS = 900


def app_endpoint(client: WorkspaceClient) -> str:
    """Return the app URL once Databricks reports the app compute as active."""
    print(f"Waiting for {APP_NAME}")
    app = client.apps.wait_get_app_active(
        name=APP_NAME,
        timeout=timedelta(seconds=TIMEOUT_SECONDS),
    )
    if not app.url:
        raise RuntimeError(f"{APP_NAME} is ACTIVE but has no URL")
    return app.url


def wait_until_healthy(client: WorkspaceClient, base_url: str) -> None:
    """Poll ``/health`` until Ontop reports ready, authenticating as the user."""
    health_url = f"{base_url.rstrip('/')}/health"
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_status = "no response"
    with httpx.Client(timeout=15.0) as http:
        while time.monotonic() < deadline:
            try:
                response = http.get(health_url, headers=client.config.authenticate())
                payload = response.json()
                if response.status_code == 200 and payload.get("status") == "ok":
                    print(f"App ready: {base_url}", flush=True)
                    return
                last_status = f"HTTP {response.status_code}; body={payload!r}"
            except (httpx.HTTPError, ValueError) as exc:
                last_status = f"health unavailable ({type(exc).__name__})"
            print(f"Waiting for {APP_NAME} health: {last_status}", flush=True)
            time.sleep(10)
    raise TimeoutError(
        f"{APP_NAME} did not become healthy within {TIMEOUT_SECONDS}s "
        f"(last status: {last_status})"
    )


def bearer_token(client: WorkspaceClient) -> str:
    """Extract the current user bearer token from the SDK auth headers."""
    authorization = client.config.authenticate().get("Authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise RuntimeError(
            "Databricks SDK did not return a bearer token; authenticate with "
            "`databricks auth login` for this profile."
        )
    return authorization[len("bearer ") :]


def run_tests(endpoint: str, token: str) -> int:
    test_env = os.environ.copy()
    test_env["SHACL_IT_ENDPOINT"] = endpoint
    test_env["SHACL_IT_TOKEN"] = token
    command = [
        sys.executable,
        "-m",
        "pytest",
        "shacl/tests/test_shacl_ontop.py",
        "-m",
        "integration",
        "-q",
    ]
    return subprocess.run(command, cwd=APP_DIR, env=test_env).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()

    client = WorkspaceClient(profile=args.profile)
    base_url = app_endpoint(client)
    wait_until_healthy(client, base_url)
    return run_tests(base_url, bearer_token(client))


if __name__ == "__main__":
    raise SystemExit(main())
