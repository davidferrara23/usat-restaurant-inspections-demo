CREATE TABLE restaurants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  city TEXT NOT NULL,
  state TEXT NOT NULL,
  county TEXT,
  newsroom TEXT,
  lastInspected DATE,
  lastInspectionId INTEGER,
  lastUpdated DATETIME,
  yelpReviewsCount INTEGER,
  yelpCuisine TEXT,
  priorCoverage TEXT
);

CREATE TABLE inspections (
  id INTEGER PRIMARY KEY,
  restaurantId INTEGER NOT NULL,
  score TEXT,
  grade TEXT,
  date DATE NOT NULL,
  details TEXT,
  FOREIGN KEY (restaurantId) REFERENCES restaurants(id)
);

CREATE INDEX idx_restaurants_city ON restaurants(city);
CREATE INDEX idx_restaurants_newsroom ON restaurants(newsroom);
CREATE INDEX idx_inspections_restaurantId ON inspections(restaurantId);
CREATE INDEX idx_inspections_date ON inspections(date);