"""Generate a newsworthy payload from the database and send it to a flow URL.

The script builds the same event envelope as agent/sample_payload.json using the
latest inspection in the database by default, then posts that JSON to a Copilot
Studio flow or webhook endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BASE_DIR / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db import create_db_and_tables, engine  # type: ignore  # noqa: E402
from generate_sample_payload import build_payload, _pick_target  # type: ignore  # noqa: E402
from sqlmodel import Session  # type: ignore  # noqa: E402


DEFAULT_FLOW_URL_ENV = "COPILOT_STUDIO_FLOW_URL"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a generated inspection payload to a Copilot Studio flow")
    parser.add_argument("--flow-url", help=f"Target flow/webhook URL. Defaults to ${DEFAULT_FLOW_URL_ENV}.")
    parser.add_argument("--restaurant-id", type=int, help="Restaurant id to use")
    parser.add_argument("--inspection-id", type=int, help="Inspection id to use")
    parser.add_argument("--market", help="Override the assignment market")
    parser.add_argument("--reporter-name", default="David Ferrara", help="Target reporter name")
    parser.add_argument("--reporter-email", default="dferrara@example.com", help="Target reporter email")
    parser.add_argument("--event-id", help="Override the event id")
    parser.add_argument("--triggered-at", help="Override the triggered_at timestamp")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Print the payload without sending it")
    return parser.parse_args()


def _resolve_flow_url(explicit_url: str | None) -> str:
    flow_url = (explicit_url or os.environ.get(DEFAULT_FLOW_URL_ENV) or "").strip()
    if not flow_url:
        raise ValueError(f"Flow URL is missing. Set --flow-url or {DEFAULT_FLOW_URL_ENV}.")
    return flow_url


def _post_json(url: str, payload: dict, timeout: int) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read().decode("utf-8", errors="replace")
        return response.status, response_body


def main() -> None:
    args = parse_args()

    create_db_and_tables()
    with Session(engine) as session:
        restaurant, inspection = _pick_target(session, args.restaurant_id, args.inspection_id)
        payload = build_payload(
            restaurant,
            inspection,
            market=args.market,
            reporter_name=args.reporter_name,
            reporter_email=args.reporter_email,
            event_id=args.event_id,
            triggered_at=args.triggered_at,
        )

    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.dry_run:
        print(rendered)
        return

    flow_url = _resolve_flow_url(args.flow_url)

    status, response_text = _post_json(flow_url, payload, args.timeout)
    print(f"POST {flow_url}")
    print(f"Status: {status}")
    if response_text:
        print(response_text)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        print(f"HTTP error: {exc.code} {exc.reason}")
        if error_body:
            print(error_body)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc