"""Import scraper JSON result files into the restaurant inspections database.

The scraper output is restaurant-shaped, so this script upserts restaurants and
creates or updates a single inspection row per restaurant/date when a valid
inspection date is present.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from db import create_db_and_tables, engine
from models import Inspection, Restaurant


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None

    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _is_date_like(value: Any) -> bool:
    return _parse_date(value) is not None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def _load_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]

    if isinstance(payload, dict):
        for key in ("results", "data", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]

    raise ValueError(f"Unsupported JSON shape in {path}")


def _expand_inputs(inputs: list[str]) -> list[Path]:
    resolved: list[Path] = []

    for raw in inputs:
        candidate = Path(raw)
        if candidate.is_dir():
            resolved.extend(sorted(candidate.glob("results_*.json")))
            continue

        if candidate.exists():
            resolved.append(candidate)
            continue

        matches = sorted(Path().glob(raw))
        if matches:
            resolved.extend(matches)
            continue

        raise FileNotFoundError(f"No input files matched: {raw}")

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in resolved:
        absolute = path.resolve()
        if absolute in seen:
            continue
        seen.add(absolute)
        unique_paths.append(absolute)

    return unique_paths


def _restaurant_key(record: dict[str, Any], default_state: str | None) -> tuple[str, str, str, str]:
    name = str(record.get("name") or "").strip()
    address = str(record.get("address") or "").strip()
    city = str(record.get("city") or "").strip()
    state = str(record.get("state") or default_state or "").strip().upper()
    return name, address, city, state


def _infer_inspection_date(record: dict[str, Any]) -> date | None:
    direct_date = _parse_date(record.get("lastInspected"))
    if direct_date is not None:
        return direct_date

    details = record.get("details")
    if isinstance(details, dict):
        for key in ("date", "inspectionDate", "lastInspected", "col_4", "col_5", "col_6"):
            inferred = _parse_date(details.get(key))
            if inferred is not None:
                return inferred

        for value in details.values():
            inferred = _parse_date(value)
            if inferred is not None:
                return inferred

    return None


def _inspection_details(record: dict[str, Any], source_file: str) -> str:
    details = record.get("details")
    if not isinstance(details, dict):
        details = {}

    payload = {
        "source_file": source_file,
        "source_lastInspectionId": record.get("lastInspectionId"),
        "source_details": details,
    }
    return json.dumps(payload, ensure_ascii=False)


def _maybe_grade(details: dict[str, Any]) -> str | None:
    for key in ("grade", "inspectionGrade", "col_3", "col_5"):
        value = details.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if re.fullmatch(r"[A-F][+-]?", text.upper()):
            return text.upper()
    return None


def upsert_record(
    session: Session,
    record: dict[str, Any],
    *,
    default_state: str | None,
    newsroom: str | None,
    source_file: str,
) -> tuple[bool, bool]:
    """Insert or update a restaurant and its latest inspection row.

    Returns a tuple of (restaurant_created_or_updated, inspection_created_or_updated).
    """
    name, address, city, state = _restaurant_key(record, default_state)
    if not (name and address and city and state):
        raise ValueError("Missing required restaurant fields")

    county = record.get("county")
    county_text = str(county).strip() if county not in (None, "") else None
    if county_text in {None, "", "None"} or _is_date_like(county_text):
        county_text = None

    record_last_inspected = _infer_inspection_date(record)
    source_last_inspection_id = _parse_int(record.get("lastInspectionId"))

    stmt = select(Restaurant).where(
        Restaurant.name == name,
        Restaurant.address == address,
        Restaurant.city == city,
        Restaurant.state == state,
    )
    restaurant = session.exec(stmt).first()
    if restaurant is None:
        restaurant = Restaurant(
            name=name,
            address=address,
            city=city,
            state=state,
            county=county_text,
            newsroom=newsroom,
            lastInspected=record_last_inspected,
            lastInspectionId=source_last_inspection_id,
            lastUpdated=_utc_now(),
        )
        session.add(restaurant)
        session.commit()
        session.refresh(restaurant)
    else:
        restaurant.address = address
        restaurant.city = city
        restaurant.state = state
        if county_text is not None:
            restaurant.county = county_text
        if newsroom is not None:
            restaurant.newsroom = newsroom
        if record_last_inspected is not None:
            restaurant.lastInspected = record_last_inspected
        if source_last_inspection_id is not None:
            restaurant.lastInspectionId = source_last_inspection_id
        restaurant.lastUpdated = _utc_now()
        session.add(restaurant)
        session.commit()
        session.refresh(restaurant)

    inspection_created_or_updated = False
    if record_last_inspected is not None:
        details_obj = record.get("details")
        if not isinstance(details_obj, dict):
            details_obj = {}

        inspection_details = _inspection_details(record, source_file)
        grade = _maybe_grade(details_obj)

        inspection_stmt = select(Inspection).where(
            Inspection.restaurantId == restaurant.id,
            Inspection.date == record_last_inspected,
            Inspection.details == inspection_details,
        )
        inspection = session.exec(inspection_stmt).first()
        if inspection is None:
            inspection = Inspection(
                restaurantId=restaurant.id,
                date=record_last_inspected,
                grade=grade,
                details=inspection_details,
            )
            session.add(inspection)
            session.commit()
            session.refresh(inspection)
        else:
            inspection.grade = grade or inspection.grade
            inspection.details = inspection_details
            session.add(inspection)
            session.commit()
            session.refresh(inspection)

        restaurant.lastInspected = inspection.date
        restaurant.lastInspectionId = inspection.id
        restaurant.lastUpdated = _utc_now()
        session.add(restaurant)
        session.commit()
        session.refresh(restaurant)
        inspection_created_or_updated = True

    return True, inspection_created_or_updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import scraper JSON results into the database")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["..\\scraper\\results_*.json"],
        help="One or more JSON files, directories, or glob patterns",
    )
    parser.add_argument(
        "--default-state",
        dest="default_state",
        help="Fallback state code for records that do not include one",
    )
    parser.add_argument(
        "--newsroom",
        help="Optional newsroom value to store on imported restaurants",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    create_db_and_tables()
    input_files = _expand_inputs(args.paths)

    restaurant_count = 0
    inspection_count = 0
    skipped_count = 0

    with Session(engine) as session:
        for path in input_files:
            logger.info("Importing %s", path)
            try:
                records = _load_records(path)
            except Exception as exc:
                logger.error("Skipping %s: %s", path, exc)
                continue

            for record in records:
                try:
                    _, created_inspection = upsert_record(
                        session,
                        record,
                        default_state=args.default_state,
                        newsroom=args.newsroom,
                        source_file=path.name,
                    )
                    restaurant_count += 1
                    if created_inspection:
                        inspection_count += 1
                except Exception as exc:
                    skipped_count += 1
                    logger.warning("Skipping record in %s: %s", path.name, exc)

    logger.info(
        "Import complete: %d restaurant rows upserted, %d inspection rows upserted, %d records skipped",
        restaurant_count,
        inspection_count,
        skipped_count,
    )
    print(
        f"Imported {restaurant_count} restaurant rows, {inspection_count} inspection rows, {skipped_count} skipped"
    )


if __name__ == "__main__":
    main()