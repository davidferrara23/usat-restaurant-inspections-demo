# models.py
from datetime import date
from datetime import date as date_type, datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


class RestaurantBase(SQLModel):
    name: str = Field(index=True)
    address: str
    city: str = Field(index=True)
    state: str = Field(index=True, max_length=2)
    county: Optional[str] = Field(default=None, index=True)
    newsroom: Optional[str] = Field(default=None, index=True)

    lastInspected: Optional[date] = Field(default=None, index=True)
    lastInspectionId: Optional[int] = Field(default=None, index=True)
    lastUpdated: Optional[datetime] = Field(default=None, index=True)

    yelpReviewsCount: Optional[int] = Field(default=None)
    yelpCuisine: Optional[str] = Field(default=None)
    priorCoverage: Optional[str] = Field(default=None)


class Restaurant(RestaurantBase, table=True):
    __tablename__ = "restaurants"

    id: Optional[int] = Field(default=None, primary_key=True)

    inspections: list["Inspection"] = Relationship(back_populates="restaurant")


class InspectionBase(SQLModel):
    restaurantId: int = Field(foreign_key="restaurants.id", index=True)
    score: Optional[str] = None
    grade: Optional[str] = Field(default=None, index=True)
    date: date_type = Field(index=True)
    details: Optional[str] = None


class Inspection(InspectionBase, table=True):
    __tablename__ = "inspections"

    id: Optional[int] = Field(default=None, primary_key=True)

    restaurant: Optional[Restaurant] = Relationship(back_populates="inspections")


# Create/update/read schemas for FastAPI

class RestaurantCreate(RestaurantBase):
    pass


class RestaurantRead(RestaurantBase):
    id: int


class InspectionCreate(InspectionBase):
    pass


class InspectionRead(InspectionBase):
    id: int


class RestaurantReadWithInspections(RestaurantRead):
    inspections: list[InspectionRead] = []


class InspectionReadWithRestaurant(InspectionRead):
    restaurant: Optional[RestaurantRead] = None