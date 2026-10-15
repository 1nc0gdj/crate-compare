-- Record comparison site: SQLite schema
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  mbid      TEXT,            -- MusicBrainz ID (optional, helps de-duplicate)
  discogs_id INTEGER
);

CREATE TABLE IF NOT EXISTS labels (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  mbid      TEXT,
  discogs_id INTEGER
);

-- What you follow. One row per artist or label.
CREATE TABLE IF NOT EXISTS follows (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('artist','label')),
  entity_id   INTEGER NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (entity_type, entity_id)
);

-- One row per release, regardless of how many shops sell it.
CREATE TABLE IF NOT EXISTS releases (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  label_id      INTEGER REFERENCES labels(id),
  catalogue_no  TEXT,         -- normalised: uppercase, no spaces/hyphens
  format        TEXT,         -- '12"', '2x12"', 'LP', 'CD', 'Digital'
  release_date  TEXT,         -- ISO date; future date = upcoming
  artwork_url   TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (label_id, catalogue_no, format)
);

CREATE TABLE IF NOT EXISTS release_artists (
  release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
  artist_id  INTEGER NOT NULL REFERENCES artists(id),
  PRIMARY KEY (release_id, artist_id)
);

CREATE TABLE IF NOT EXISTS retailers (
  id                 INTEGER PRIMARY KEY,
  slug               TEXT NOT NULL UNIQUE,   -- 'juno', 'deejay', 'yoyaku'...
  name               TEXT NOT NULL,
  base_url           TEXT NOT NULL,
  currency           TEXT NOT NULL,          -- retailer's native currency
  -- Shipping to Australia, maintained by hand (it's cart-dependent, so not scraped)
  ship_first_item    REAL,                   -- cost for first record
  ship_extra_item    REAL,                   -- cost per additional record
  free_ship_over     REAL                    -- NULL if none
);

-- One row per release per retailer.
CREATE TABLE IF NOT EXISTS listings (
  id           INTEGER PRIMARY KEY,
  release_id   INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
  retailer_id  INTEGER NOT NULL REFERENCES retailers(id),
  url          TEXT NOT NULL,
  price        REAL,
  currency     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'in_stock'
               CHECK (status IN ('in_stock','preorder','sold_out','unknown')),
  previews     TEXT,         -- JSON list of {title, url} direct audio links, when a shop offers them
  genres       TEXT,         -- JSON list of the shop's own genre labels
  preview_url  TEXT,         -- retailer's player page or audio URL; link out, don't host
  first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (release_id, retailer_id)
);

-- Optional: lets you show "price dropped" or "back in stock"
CREATE TABLE IF NOT EXISTS price_history (
  id         INTEGER PRIMARY KEY,
  listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  price      REAL,
  status     TEXT,
  seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_releases_date ON releases(release_date);
CREATE INDEX IF NOT EXISTS idx_listings_release ON listings(release_id);

-- The page query: releases from things you follow, newest first,
-- with the cheapest listing per release.
--
-- SELECT r.*, l.name AS label, MIN(li.price) AS best_price
-- FROM releases r
-- JOIN labels l ON l.id = r.label_id
-- JOIN listings li ON li.release_id = r.id
-- WHERE r.label_id IN (SELECT entity_id FROM follows WHERE entity_type='label')
--    OR r.id IN (
--         SELECT ra.release_id FROM release_artists ra
--         JOIN follows f ON f.entity_type='artist' AND f.entity_id = ra.artist_id)
-- GROUP BY r.id
-- ORDER BY r.release_date DESC;

-- Where each label's own page lives at each shop (used later to fetch a followed label's full catalogue)
CREATE TABLE IF NOT EXISTS label_pages (
  label_id    INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  retailer_id INTEGER NOT NULL REFERENCES retailers(id),
  url         TEXT NOT NULL,
  PRIMARY KEY (label_id, retailer_id)
);
