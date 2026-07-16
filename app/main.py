# main.py
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Session, select

from db import create_db_and_tables, get_session
from models import (
    Restaurant,
    RestaurantCreate,
    RestaurantRead,
    RestaurantReadWithInspections,
    Inspection,
    InspectionCreate,
    InspectionRead,
    InspectionReadWithRestaurant,
)

app = FastAPI(title="Restaurant Inspections API")

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

@app.post("/restaurants", response_model=RestaurantRead)
def create_restaurant(
    restaurant: RestaurantCreate,
    session: Session = Depends(get_session),
):
    db_restaurant = Restaurant.model_validate(restaurant)
    session.add(db_restaurant)
    session.commit()
    session.refresh(db_restaurant)
    return db_restaurant

@app.get("/restaurants", response_model=list[RestaurantRead])
def list_restaurants(
    city: str | None = None,
    newsroom: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, le=500),
    session: Session = Depends(get_session),
):
    stmt = select(Restaurant)

    if city:
        stmt = stmt.where(Restaurant.city == city)
    if newsroom:
        stmt = stmt.where(Restaurant.newsroom == newsroom)
    if q:
        stmt = stmt.where(Restaurant.name.contains(q))

    stmt = stmt.limit(limit)
    return session.exec(stmt).all()

@app.get("/restaurants/{restaurant_id}", response_model=RestaurantReadWithInspections)
def get_restaurant(
    restaurant_id: int,
    session: Session = Depends(get_session),
):
    restaurant = session.get(Restaurant, restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return restaurant

@app.post("/inspections", response_model=InspectionRead)
def create_inspection(
    inspection: InspectionCreate,
    session: Session = Depends(get_session),
):
    restaurant = session.get(Restaurant, inspection.restaurantId)
    if not restaurant:
        raise HTTPException(status_code=400, detail="restaurantId does not exist")

    db_inspection = Inspection.model_validate(inspection)
    session.add(db_inspection)
    session.commit()
    session.refresh(db_inspection)

    restaurant.lastInspected = db_inspection.date
    restaurant.lastInspectionId = db_inspection.id
    session.add(restaurant)
    session.commit()
    session.refresh(db_inspection)

    return db_inspection

@app.get("/inspections", response_model=list[InspectionRead])
def list_inspections(
    restaurant_id: int | None = None,
    grade: str | None = None,
    limit: int = Query(default=100, le=500),
    session: Session = Depends(get_session),
):
    stmt = select(Inspection)

    if restaurant_id:
        stmt = stmt.where(Inspection.restaurantId == restaurant_id)
    if grade:
        stmt = stmt.where(Inspection.grade == grade)

    stmt = stmt.limit(limit)
    return session.exec(stmt).all()

@app.get("/inspections/{inspection_id}", response_model=InspectionReadWithRestaurant)
def get_inspection(
    inspection_id: int,
    session: Session = Depends(get_session),
):
    inspection = session.get(Inspection, inspection_id)
    if not inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
    return inspection