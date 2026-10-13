CREATE TABLE IF NOT EXISTS records (
  collection TEXT NOT NULL,
  record_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (collection, record_id)
);
