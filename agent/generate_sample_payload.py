"""Generate a sample newsworthy inspection payload from the database.

This script reads the local SQLite database used by the project and emits a
payload that matches agent/sample_payload.json as closely as the stored data
allows.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BASE_DIR / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db import create_db_and_tables, engine  # type: ignore  # noqa: E402
from models import Inspection, Restaurant  # type: ignore  # noqa: E402
from sqlmodel import Session, select  # type: ignore  # noqa: E402


DEFAULT_REPORTER_NAME = "David Ferrara"
DEFAULT_REPORTER_EMAIL = "dferrara@example.com"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _latest_inspection_for_restaurant(session: Session, restaurant_id: int) -> Inspection | None:
    stmt = (
        select(Inspection)
        .where(Inspection.restaurantId == restaurant_id)
        .order_by(Inspection.date.desc(), Inspection.id.desc())
    )
    return session.exec(stmt).first()


def _pick_target(session: Session, restaurant_id: int | None, inspection_id: int | None) -> tuple[Restaurant, Inspection]:
    if inspection_id is not None:
        inspection = session.get(Inspection, inspection_id)
        if inspection is None:
            raise ValueError(f"Inspection not found: {inspection_id}")

        restaurant = session.get(Restaurant, inspection.restaurantId)
        if restaurant is None:
            raise ValueError(f"Restaurant not found for inspection: {inspection_id}")
        return restaurant, inspection

    if restaurant_id is not None:
        restaurant = session.get(Restaurant, restaurant_id)
        if restaurant is None:
            raise ValueError(f"Restaurant not found: {restaurant_id}")

        inspection = _latest_inspection_for_restaurant(session, restaurant_id)
        if inspection is None:
            raise ValueError(f"No inspections found for restaurant: {restaurant_id}")
        return restaurant, inspection

    inspection = session.exec(select(Inspection).order_by(Inspection.date.desc(), Inspection.id.desc())).first()
    if inspection is None:
        raise ValueError("No inspections found in the database")

    restaurant = session.get(Restaurant, inspection.restaurantId)
    if restaurant is None:
        raise ValueError(f"Restaurant not found for inspection: {inspection.id}")
    return restaurant, inspection


def _normalize_coverage_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return {
            "headline": item.get("headline"),
            "publisher": item.get("publisher"),
            "published_at": item.get("published_at"),
            "url": item.get("url"),
            "sentiment": item.get("sentiment"),
        }

    text = str(item).strip() if item is not None else ""
    if not text:
        return None

    return {
        "headline": text,
        "publisher": None,
        "published_at": None,
        "url": None,
        "sentiment": None,
    }


def _prior_coverage(restaurant: Restaurant) -> list[dict[str, Any]]:
    parsed = _parse_json(restaurant.priorCoverage)
    if isinstance(parsed, list):
        return [item for item in (_normalize_coverage_item(entry) for entry in parsed) if item is not None]
    if parsed is not None:
        normalized = _normalize_coverage_item(parsed)
        return [normalized] if normalized is not None else []

    if restaurant.priorCoverage:
        normalized = _normalize_coverage_item(restaurant.priorCoverage)
        return [normalized] if normalized is not None else []

    return []


def _source_details(inspection: Inspection) -> dict[str, Any]:
    parsed = _parse_json(inspection.details)
    if isinstance(parsed, dict):
        return parsed.get("source_details") if isinstance(parsed.get("source_details"), dict) else parsed
    return {}


def _extract_list_of_dicts(source: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(source, dict):
        return []

    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            items: list[dict[str, Any]] = []
            for entry in value:
                if isinstance(entry, dict):
                    items.append(entry)
            if items:
                return items
    return []


def _normalize_violations(source: dict[str, Any]) -> list[dict[str, Any]]:
    items = _extract_list_of_dicts(source, ("violations", "critical_violations", "inspection_violations"))
    normalized: list[dict[str, Any]] = []
    for item in items:
        normalized.append(
            {
                "title": item.get("title"),
                "severity": item.get("severity"),
                "corrected_on_site": bool(item.get("corrected_on_site", False)),
                "description": item.get("description"),
            }
        )
    return normalized


def _normalize_complaints(source: dict[str, Any]) -> list[dict[str, Any]]:
    items = _extract_list_of_dicts(source, ("complaints", "complaint", "public_complaints"))
    normalized: list[dict[str, Any]] = []
    for item in items:
        normalized.append(
            {
                "source": item.get("source"),
                "summary": item.get("summary"),
                "verified": bool(item.get("verified", False)),
            }
        )
    return normalized


def _infer_inspection_type(source: dict[str, Any], fallback: str = "Inspection") -> str:
    text_bits = []
    for key in ("inspection_type", "type", "result", "inspection_result"):
        value = source.get(key)
        if isinstance(value, str):
            text_bits.append(value.lower())

    for value in source.values():
        if isinstance(value, str):
            text_bits.append(value.lower())

    joined = " ".join(text_bits)
    if "complaint" in joined:
        return "Complaint"
    if "follow-up" in joined or "follow up" in joined:
        return "Follow-up"
    if "routine" in joined:
        return "Routine"
    return fallback


def _infer_inspection_result(inspection: Inspection, source: dict[str, Any]) -> str:
    explicit = source.get("inspection_result")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    grade = (inspection.grade or "").upper()
    score = _safe_int(inspection.score)

    if "follow-up" in " ".join(str(v).lower() for v in source.values() if isinstance(v, str)):
        return "Follow-up required"
    if grade in {"D", "F"}:
        return "Follow-up required"
    if score is not None and score < 85:
        return "Review required"
    if grade in {"A", "B"} or (score is not None and score >= 85):
        return "Passed"
    return "Review needed"


def _summary_for_payload(restaurant: Restaurant, inspection: Inspection, inspection_type: str, inspection_result: str) -> str:
    date_text = inspection.date.isoformat() if isinstance(inspection.date, date) else str(inspection.date)
    city = restaurant.city or "the area"
    state = restaurant.state or ""
    return (
        f"{inspection_type} inspection for {restaurant.name} in {city}, {state} on {date_text} "
        f"was flagged as {inspection_result.lower()}."
    ).strip()


def build_payload(
    restaurant: Restaurant,
    inspection: Inspection,
    *,
    market: str | None,
    reporter_name: str,
    reporter_email: str,
    event_id: str | None = None,
    triggered_at: str | None = None,
) -> dict[str, Any]:
    source = _source_details(inspection)
    inspection_type = _infer_inspection_type(source)
    inspection_result = _infer_inspection_result(inspection, source)
    official_score = _safe_int(inspection.score)

    payload_event_id = event_id or f"evt_{restaurant.state.lower()}_{inspection.date.strftime('%Y%m%d')}_{inspection.id}"
    payload_triggered_at = triggered_at or _utc_now_iso()

    return {
        "event_id": payload_event_id,
        "triggered_at": payload_triggered_at,
        "assignment": {
            "market": market or restaurant.newsroom or restaurant.city,
            "state": restaurant.state,
            "target_reporters": [
                {
                    "name": reporter_name,
                    "email": reporter_email,
                }
            ],
        },
        "inspection": {
            "inspection_id": f"inspection-{inspection.id}",
            "establishment_name": restaurant.name,
            "address": {
                "street": restaurant.address,
                "city": restaurant.city,
                "county": restaurant.county,
                "state": restaurant.state,
                "zip": None,
            },
            "inspection_date": inspection.date.isoformat(),
            "inspection_type": inspection_type,
            "inspection_result": inspection_result,
            "closure_status": "Open",
            "official_score": official_score,
            "official_url": None,
            "summary": _summary_for_payload(restaurant, inspection, inspection_type, inspection_result),
            "violations": _normalize_violations(source),
            "complaints": _normalize_complaints(source),
        },
        "context": {
            "prior_coverage": _prior_coverage(restaurant),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a sample payload from the restaurant inspection database")
    parser.add_argument("--restaurant-id", type=int, help="Restaurant id to use")
    parser.add_argument("--inspection-id", type=int, help="Inspection id to use")
    parser.add_argument("--output", type=Path, help="Write the payload to this file instead of stdout")
    parser.add_argument("--market", help="Override the assignment market")
    parser.add_argument("--reporter-name", default=DEFAULT_REPORTER_NAME, help="Target reporter name")
    parser.add_argument("--reporter-email", default=DEFAULT_REPORTER_EMAIL, help="Target reporter email")
    parser.add_argument("--event-id", help="Override the event id")
    parser.add_argument("--triggered-at", help="Override the triggered_at timestamp")
    return parser.parse_args()


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
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
