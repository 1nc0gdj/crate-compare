"""
Crate Compare: scraper framework + first retailer adapter (Juno).

Usage:
    python scraper.py init            # create DB from schema.sql, seed retailers
    python scraper.py juno            # scrape Juno\n    python scraper.py deejay          # scrape Deejay.de\n    python scraper.py yoyaku          # scrape Yoyaku\n    python scraper.py all             # scrape every shop\n    python scraper.py compare         # prices delivered to Sydney, in AUD

The Juno adapter reads link addresses and text patterns from the page.
"""
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DB_PATH = "crate.db"
HEADERS = {"User-Agent": "CrateCompare/0.1 (personal project; contact: you@example.com)"}
DELAY_SECONDS = 3  # be polite: one request every few seconds


# ---------- Normalised record every adapter must produce ----------
@dataclass
class Listing:
    retailer_slug: str
    title: str
    artists: list[str]
    label: str
    catalogue_no: str | None
    format: str | None
    price: float | None
    currency: str
    status: str                 # in_stock | preorder | sold_out | unknown
    url: str
    preview_url: str | None = None
    artwork_url: str | None = None
    release_date: str | None = None   # ISO yyyy-mm-dd


def norm_cat(cat: str | None) -> str | None:
    """'XYZ-012' / 'xyz 012' -> 'XYZ012' so the same release matches across shops."""
    if not cat:
        return None
    return re.sub(r"[\s\-_/.]", "", cat).upper()


def parse_price(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", text.replace(",", "."))
    return float(m.group(1)) if m else None


# ---------- Adapter base ----------
class Adapter:
    slug = ""

    def fetch(self, url: str) -> BeautifulSoup:
        time.sleep(DELAY_SECONDS)
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    def scrape(self, label_or_artist_urls: list[str]) -> list[Listing]:
        raise NotImplementedError


# ---------- Juno adapter ----------
class JunoAdapter(Adapter):
    """
    Works from patterns in Juno's page (link addresses and text), not CSS class
    names, so it's less likely to break if they restyle the site.
    Each release block has: a /products/ link, /artists/ and /labels/ links,
    text like 'Cat: AF 069. Rel: 08 Oct 26', /MP3/ preview links and a price.
    """
    slug = "juno"
    BASE = "https://www.juno.co.uk"
    CURRENCY = {"$": "USD", "\u00a3": "GBP", "\u20ac": "EUR"}

    def scrape(self, urls):
        out = []
        for url in urls:
            try:
                soup = self.fetch(url)
            except requests.RequestException as e:
                print(f"  {url} -> could not open ({e}); skipping")
                continue
            blocks = self.release_blocks(soup)
            print(f"  {url} -> found {len(blocks)} releases")
            for href, node in blocks:
                item = self.parse_block(href, node)
                if item:
                    out.append(item)
        return out

    def release_blocks(self, soup):
        """Find each release's box by climbing up from its product link until
        the box would start containing a different release."""
        blocks, seen = [], set()
        for a in soup.select('a[href*="/products/"]'):
            href = a["href"]
            if href in seen:
                continue
            node = a
            while True:
                text = node.get_text(" ", strip=True)
                if "Cat:" in text and re.search(r"[$\u00a3\u20ac]\s?\d[\d,]*\.\d{2}", text):
                    break  # this box has everything: stop before swallowing the menus
                parent = node.parent
                if not parent or parent.name in ("body", "html"):
                    break
                if len({x["href"] for x in parent.select('a[href*="/products/"]')}) > 1:
                    break
                node = parent
            if "Cat:" in node.get_text(" ", strip=True):
                seen.add(href)
                blocks.append((href, node))
        return blocks

    def parse_block(self, href, node):
        text = node.get_text(" ", strip=True)

        # Title: the product link that has words in it (the other one is the image)
        title = ""
        for a in node.select('a[href*="/products/"]'):
            if a.get_text(strip=True):
                title = a.get_text(strip=True)
                break
        if not title:
            return None

        fmt = re.search(re.escape(title) + r"\s*\(([^)]+)\)", text)
        cat = re.search(r"Cat:\s*(.*?)\.\s*Rel:\s*(\d{1,2}\s+\w{3}\s+\d{2})", text)
        price = re.search(r"([$\u00a3\u20ac])\s?(\d[\d,]*\.\d{2})", text)

        release_date = None
        if cat:
            try:
                release_date = datetime.strptime(cat.group(2), "%d %b %y").date().isoformat()
            except ValueError:
                pass

        low = text.lower()
        if "out of stock" in low:
            status = "sold_out"
        elif "preorder" in low or "pre-order" in low or (
            release_date and release_date > date.today().isoformat()
        ):
            status = "preorder"
        elif "in stock" in low or "low stock" in low:
            status = "in_stock"
        else:
            status = "unknown"

        label = next((a for a in node.select('a[href*="/labels/"]')
                      if "?" not in a["href"] and a.get_text(strip=True)), None)
        preview = node.select_one('a[href*="/MP3/"]')
        img = node.select_one("img")

        return Listing(
            retailer_slug=self.slug,
            title=title,
            artists=[a.get_text(strip=True) for a in node.select('a[href*="/artists/"]')
                     if a.get_text(strip=True)],
            label=label.get_text(strip=True) if label else "",
            catalogue_no=norm_cat(cat.group(1)) if cat else None,
            format=fmt.group(1) if fmt else None,
            price=float(price.group(2).replace(",", "")) if price else None,
            currency=self.CURRENCY.get(price.group(1), "USD") if price else "USD",
            status=status,
            url=urljoin(self.BASE, href),
            preview_url=urljoin(self.BASE, preview["href"]) if preview else None,
            artwork_url=img["src"] if img and img.has_attr("src") else None,
            release_date=release_date,
        )


# ---------- Deejay.de adapter ----------
class DeejayAdapter(Adapter):
    """
    Deejay.de: prices in EUR with comma decimals ('12,40 EUR'), release date as
    dd.mm.yyyy, format as '12inch'. Audio previews are loaded by JavaScript, so
    preview_url points at the release page (which has the player).
    """
    slug = "deejay"
    BASE = "https://www.deejay.de"
    PRODUCT = re.compile(r"_[A-Za-z]+__\d+$")   # .../Artist_Title_CAT_Vinyl__1101818
    LABEL = re.compile(r"__L\d+$")              # .../MALoeR_Records__L296645

    def scrape(self, urls):
        out = []
        for url in urls:
            try:
                soup = self.fetch(url)
            except requests.RequestException as e:
                print(f"  {url} -> could not open ({e}); skipping")
                continue
            blocks = self.release_blocks(soup)
            print(f"  {url} -> found {len(blocks)} releases")
            for href, node in blocks:
                item = self.parse_block(href, node)
                if item:
                    out.append(item)
        return out

    def product_links(self, node):
        return [a for a in node.select("a[href]") if self.PRODUCT.search(a["href"])]

    def release_blocks(self, soup):
        blocks, seen = [], set()
        for a in self.product_links(soup):
            href = a["href"]
            if href in seen:
                continue
            node = a
            while True:
                text = node.get_text(" ", strip=True)
                if "inch" in text and re.search(r"\d,\d{2}\s*\u20ac", text):
                    break  # this box has everything: stop before swallowing the menus
                parent = node.parent
                if not parent or parent.name in ("body", "html"):
                    break
                if len({x["href"] for x in self.product_links(parent)}) > 1:
                    break
                node = parent
            if "inch" in node.get_text(" ", strip=True):
                seen.add(href)
                blocks.append((href, node))
        return blocks

    def parse_block(self, href, node):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))  # also turns non-breaking spaces into normal ones

        title_el = node.select_one("h3 a") or next(
            (a for a in self.product_links(node) if a.get_text(strip=True)), None)
        if not title_el:
            return None
        title = title_el.get_text(strip=True)

        artists = [a.get_text(strip=True) for a in node.select("h2 a") if a.get_text(strip=True)]
        label_el = next((a for a in node.select("a[href]") if self.LABEL.search(a["href"])), None)
        label = label_el.get_text(strip=True) if label_el else ""

        # Catalogue number sits just before the label name: 'MLRVNL004 MALoeR Records 12inch ...'
        cat = None
        if label:
            m = re.search(r"(\S+)\s+" + re.escape(label) + r"\s*\d*x?\d+inch", text)
            cat = m.group(1) if m else None

        fmt = re.search(r"((?:\d+x)?\d+)inch", text)
        fmt = fmt.group(1) + '"' if fmt else None

        d = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
        release_date = None
        if d:
            try:
                release_date = datetime.strptime(d.group(1), "%d.%m.%Y").date().isoformat()
            except ValueError:
                pass

        # Last price in the block is the main one ('also available' prices come first)
        prices = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*\u20ac", text)
        price = float(prices[-1].replace(".", "").replace(",", ".")) if prices else None

        low = text.lower()
        if "out of stock" in low:
            status = "sold_out"
        elif "pre-order" in low or "shipping from" in low:
            status = "preorder"
        elif "stock from" in low or "in stock" in low or "ready to ship" in low:
            status = "in_stock"
        else:
            status = "unknown"
            print(f"  ? could not read the stock status for '{title}'. End of its text: {text[-160:]}")

        img = node.select_one("img")
        url = urljoin(self.BASE, href)
        return Listing(
            retailer_slug=self.slug, title=title, artists=artists, label=label,
            catalogue_no=norm_cat(cat), format=fmt, price=price, currency="EUR",
            status=status, url=url, preview_url=url,
            artwork_url=img["src"] if img and img.has_attr("src") else None,
            release_date=release_date,
        )


# ---------- Yoyaku adapter ----------
class YoyakuAdapter(Adapter):
    """
    Yoyaku (yoyaku.io, Paris). Each release shows 'CATALOGUE | Label', the styles and
    format ('12 Inch', '2x12"'), and a price like '14,58 EUR'. A record you can buy has an
    add-to-cart link; a sold-out one links to its release page instead. The listing doesn't
    show a release date, so that stays blank.
    """
    slug = "yoyaku"
    BASE = "https://yoyaku.io"
    PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*\u20ac")

    def scrape(self, urls):
        out = []
        for url in urls:
            try:
                soup = self.fetch(url)
            except requests.RequestException as e:
                print(f"  {url} -> could not open ({e}); skipping")
                continue
            blocks = self.release_blocks(soup)
            print(f"  {url} -> found {len(blocks)} releases")
            for href, node in blocks:
                item = self.parse_block(href, node)
                if item:
                    out.append(item)
        # The listing can't tell 'sold out' from 'pre-order', so check each release page
        print(f"  checking {len(out)} release pages for stock status (takes a couple of minutes)...")
        for item in out:
            self.confirm_status(item)
        return out

    @staticmethod
    def status_from_meta(value):
        """Yoyaku's release pages carry a 'product:availability' tag, e.g. 'preorder'."""
        v = (value or "").lower().replace("-", "").replace(" ", "")
        if "preorder" in v or "backorder" in v:
            return "preorder"
        if "outofstock" in v or v in ("oos", "soldout"):
            return "sold_out"
        if "instock" in v:
            return "in_stock"
        return None

    def confirm_status(self, item):
        try:
            soup = self.fetch(item.url)
        except requests.RequestException as e:
            print(f"    could not open {item.url} ({e}); keeping the listing's status")
            return
        meta = soup.find("meta", attrs={"property": "product:availability"})
        status = self.status_from_meta(meta.get("content") if meta else None)
        if status and status != item.status:
            print(f"    {item.title}: {item.status} -> {status}")
            item.status = status

    def label_link(self, node):
        # a plain label page link (not the add-to-cart link, which also sits under /label/)
        return next((a for a in node.select('a[href*="/label/"]')
                     if "?" not in a["href"] and a.get_text(strip=True)), None)

    def release_blocks(self, soup):
        blocks, seen = [], set()
        for a in soup.select('a[href*="/release/"]'):
            href = a["href"]
            if href in seen:
                continue
            node = a
            while True:
                if self.PRICE.search(node.get_text(" ", strip=True)) and self.label_link(node):
                    break  # has a price and a label: this is the whole release box
                parent = node.parent
                if (not parent or parent.name in ("body", "html")
                        or len({x["href"] for x in parent.select('a[href*="/release/"]')}) > 1):
                    node = None
                    break
                node = parent
            if node is not None:   # skips merch, and the small 'Top Charts' lists (no price)
                seen.add(href)
                blocks.append((href, node))
        return blocks

    def parse_block(self, href, node):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        title_el = node.select_one("h2 a") or next(
            (a for a in node.select('a[href*="/release/"]') if a.get_text(strip=True)), None)
        label_el = self.label_link(node)
        if not title_el or not label_el:
            return None
        title, label = title_el.get_text(strip=True), label_el.get_text(strip=True)

        artists = []
        for a in node.select('a[href*="/artist/"]'):
            name = a.get_text(strip=True)
            if name and name not in artists:
                artists.append(name)

        # Catalogue number: the text just before '| Label'
        prev = label_el.find_previous(string=lambda t: t.strip() not in ("", "|"))
        cat = prev.strip().rstrip("|").strip() if prev else ""
        if not cat or len(cat) > 30:
            m = re.search(r"(\S+)\s*\|\s*" + re.escape(label), text)
            cat = m.group(1) if m else ""

        tail = text.split(label, 1)[1] if label in text else text
        m = re.search(r'(?:(\d+)\s*x\s*)?(7|10|12)\s*(?:inch|"|\u2033|\u201d)', tail, re.I)
        if m:
            fmt = (m.group(1) + "x" if m.group(1) else "") + m.group(2) + '"'
        else:
            m = re.search(r"(?:(\d+)\s*x\s*)?LP\b", tail)
            fmt = ((m.group(1) + "x" if m.group(1) else "") + "LP") if m else None

        prices = self.PRICE.findall(text)
        price = float(prices[-1].replace(".", "").replace(",", ".")) if prices else None

        low = text.lower()
        if "pre-order" in low or "preorder" in low or "pre order" in low:
            status = "preorder"
        elif node.select_one('a[href*="add-to-cart="]'):
            status = "in_stock"
        else:
            status = "sold_out"

        img = node.select_one("img")
        art = (img.get("src") or img.get("data-src")) if img else None
        url = urljoin(self.BASE, href)
        return Listing(
            retailer_slug=self.slug, title=title, artists=artists, label=label,
            catalogue_no=norm_cat(cat), format=fmt, price=price, currency="EUR",
            status=status, url=url, preview_url=url, artwork_url=art, release_date=None,
        )


ADAPTERS = {"juno": JunoAdapter, "deejay": DeejayAdapter, "yoyaku": YoyakuAdapter}

# Pages to scrape: the label/artist pages for things you follow.
# Keep this in a config file or table later; hardcoded to start.
JUNO_URLS = [
    "https://www.juno.co.uk/all/this-week/",
]

YOYAKU_URLS = [
    "https://yoyaku.io/releases/",
]

DEEJAY_URLS = [
    "https://www.deejay.de/m_All/sm_ComeIn/sort_highlights/perpage_160/page_1",
]


# Shipping to Sydney, measured from each shop's real cart (in the shop's own currency).
# first = cost for 1 record, extra = added for each further record.
SHIPPING = {
    "juno":   {"first": 11.25, "extra": 3.50},   # GBP: 1 = 11.25, 2 = 14.75, 3 = 18.25
    "deejay": {"first": 17.28, "extra": 3.10},   # EUR: 1 = 17.28, 2 = 20.37, 3 = 23.47
    # Yoyaku, measured from a real cart (cheapest option, EUR): 1 = 11.00, 2 = 13.80, 3 = 15.70.
    # The extra cost isn't constant (+2.80, then +1.90), so 'extra' is the average of the two.
    "yoyaku": {"first": 11.00, "extra": 2.35},
}


# ---------- DB helpers ----------
def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect()
    con.executescript(Path("schema.sql").read_text())
    con.execute(
        """INSERT OR IGNORE INTO retailers
           (slug, name, base_url, currency, ship_first_item, ship_extra_item, free_ship_over)
           VALUES ('juno','Juno','https://www.juno.co.uk','GBP', 12.0, 2.5, NULL)"""
        # Shipping figures above are placeholders: check Juno's AU rates and edit.
    )
    con.execute(
        """INSERT OR IGNORE INTO retailers
           (slug, name, base_url, currency, ship_first_item, ship_extra_item, free_ship_over)
           VALUES ('deejay','Deejay.de','https://www.deejay.de','EUR', NULL, NULL, NULL)"""
    )
    con.execute(
        """INSERT OR IGNORE INTO retailers
           (slug, name, base_url, currency, ship_first_item, ship_extra_item, free_ship_over)
           VALUES ('yoyaku','Yoyaku','https://yoyaku.io','EUR', NULL, NULL, NULL)"""
    )
    for slug, s in SHIPPING.items():
        con.execute(
            "UPDATE retailers SET ship_first_item=?, ship_extra_item=?, free_ship_over=NULL WHERE slug=?",
            (s["first"], s["extra"], slug),
        )
    con.commit()
    print("DB ready.")


COUNTRY_WORDS = {
    "spain", "us", "usa", "uk", "germany", "netherlands", "italy", "portugal", "france",
    "belgium", "sweden", "denmark", "norway", "finland", "japan", "australia", "canada",
    "greece", "poland", "slovenia", "austria", "switzerland", "ireland", "russia", "uruguay",
    "argentina", "brazil", "mexico", "croatia", "hungary", "ukraine", "israel", "turkey",
}
FILLER_WORDS = {"records", "recordings", "recording", "rec", "label"}


def name_key(name: str) -> str:
    """Loose version of a name so 'Analogical Force Spain' == 'Analogical Force'
    and 'Hypnus Records' == 'Hypnus'."""
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in COUNTRY_WORDS:
        words.pop()
    words = [w for w in words if w not in FILLER_WORDS] or words
    return " ".join(words)


def get_or_create(con, table, name):
    key = name_key(name)
    for row in con.execute(f"SELECT id, name FROM {table}"):
        if name_key(row["name"]) == key:
            return row["id"]
    return con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid


def upsert(con, L: Listing):
    label_id = get_or_create(con, "labels", L.label) if L.label else None

    # 1) Match an existing release: catalogue number first, then title.
    rel = None
    if L.catalogue_no:
        rel = con.execute(
            "SELECT id FROM releases WHERE label_id IS ? AND catalogue_no = ?",
            (label_id, L.catalogue_no),
        ).fetchone()
    if not rel:
        rel = con.execute(
            "SELECT id FROM releases WHERE label_id IS ? AND lower(title) = lower(?)",
            (label_id, L.title),
        ).fetchone()

    if rel:
        release_id = rel["id"]
        con.execute(
            """UPDATE releases SET release_date = COALESCE(release_date, ?),
                   format = COALESCE(format, ?), artwork_url = COALESCE(artwork_url, ?)
               WHERE id = ?""",
            (L.release_date, L.format, L.artwork_url, release_id),
        )
    else:
        release_id = con.execute(
            """INSERT INTO releases (title, label_id, catalogue_no, format, release_date, artwork_url)
               VALUES (?,?,?,?,?,?)""",
            (L.title, label_id, L.catalogue_no, L.format, L.release_date, L.artwork_url),
        ).lastrowid
        for name in L.artists:
            aid = get_or_create(con, "artists", name)
            con.execute(
                "INSERT OR IGNORE INTO release_artists VALUES (?,?)", (release_id, aid)
            )

    retailer_id = con.execute(
        "SELECT id FROM retailers WHERE slug = ?", (L.retailer_slug,)
    ).fetchone()["id"]

    # 2) Upsert this retailer's listing.
    con.execute(
        """INSERT INTO listings (release_id, retailer_id, url, price, currency, status, preview_url)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(release_id, retailer_id) DO UPDATE SET
             url=excluded.url, price=excluded.price, status=excluded.status,
             preview_url=excluded.preview_url, last_seen=datetime('now')""",
        (release_id, retailer_id, L.url, L.price, L.currency, L.status, L.preview_url),
    )
    listing_id = con.execute(
        "SELECT id FROM listings WHERE release_id=? AND retailer_id=?",
        (release_id, retailer_id),
    ).fetchone()["id"]
    con.execute(
        "INSERT INTO price_history (listing_id, price, status) VALUES (?,?,?)",
        (listing_id, L.price, L.status),
    )


def load_follows():
    p = Path("follows.json")
    return json.loads(p.read_text())["labels"] if p.exists() else []


def sync_follows(con, follows):
    """Make sure every label in follows.json is in the DB and marked as followed."""
    for f in follows:
        label_id = get_or_create(con, "labels", f["name"])
        con.execute(
            "INSERT OR IGNORE INTO follows (entity_type, entity_id) VALUES ('label', ?)",
            (label_id,),
        )
    con.commit()


def run(slug):
    follows = load_follows()
    if follows:
        urls = []
        for f in follows:
            if f.get(slug):
                urls.append(f[slug])
            else:
                print(f"  {f['name']}: no {slug} page in follows.json yet; skipping")
    else:  # no follow list: fall back to the generic pages above
        urls = {"juno": JUNO_URLS, "deejay": DEEJAY_URLS, "yoyaku": YOYAKU_URLS}[slug]
    if not urls:
        print(f"{slug}: nothing to scrape.")
        return
    listings = ADAPTERS[slug]().scrape(urls)
    con = connect()
    sync_follows(con, follows)
    for L in listings:
        upsert(con, L)
    con.commit()
    print(f"{slug}: upserted {len(listings)} listings ({date.today()})")


RATE_URLS = [
    "https://api.frankfurter.dev/v1/latest?base={cur}&symbols=AUD",
    "https://api.frankfurter.app/latest?from={cur}&to=AUD",
]


def get_rates():
    """How many Australian dollars one GBP / EUR / USD is worth.
    Fetched live from a free service; the last good rates are saved in rates.json
    and used if the lookup fails (you can also edit rates.json by hand)."""
    cache = Path("rates.json")
    rates = {"AUD": 1.0}
    live = True
    for cur in ("GBP", "EUR", "USD"):
        got = None
        for url in RATE_URLS:
            try:
                r = requests.get(url.format(cur=cur), timeout=15)
                r.raise_for_status()
                got = float(r.json()["rates"]["AUD"])
                break
            except Exception:
                continue
        if got is None:
            live = False
            break
        rates[cur] = got
    if live:
        cache.write_text(json.dumps({k: v for k, v in rates.items() if k != "AUD"}, indent=2))
        print(f"Exchange rates (live): 1 GBP = A${rates['GBP']:.3f}, 1 EUR = A${rates['EUR']:.3f}")
    elif cache.exists():
        rates.update(json.loads(cache.read_text()))
        print("Could not fetch live exchange rates; using the saved ones in rates.json")
    else:
        sys.exit("Could not fetch exchange rates, and there is no rates.json. "
                 'Create one containing e.g. {"GBP": 2.0, "EUR": 1.7, "USD": 1.5} '
                 "(Australian dollars per 1 unit) and run again.")
    return rates


def compare():
    """Print each followed release with every shop's price delivered to Sydney, in AUD."""
    rates = get_rates()
    con = connect()
    rows = con.execute(
        """SELECT r.id, r.title, COALESCE(lb.name,'') AS label, r.catalogue_no,
                  rt.name AS shop, rt.currency AS ship_cur, rt.ship_first_item AS ship,
                  li.price, li.currency, li.status
           FROM releases r
           LEFT JOIN labels lb ON lb.id = r.label_id
           JOIN listings li ON li.release_id = r.id
           JOIN retailers rt ON rt.id = li.retailer_id
           WHERE r.label_id IN (SELECT entity_id FROM follows WHERE entity_type = 'label')
           ORDER BY lb.name, r.release_date DESC, r.title, rt.name"""
    ).fetchall()

    groups = {}
    for row in rows:
        groups.setdefault(row["id"], []).append(row)

    for listings in groups.values():
        first = listings[0]
        print(f"\n{first['label']}: {first['title']} ({first['catalogue_no'] or '?'})")
        options = []
        for r in listings:
            if r["price"] is None or r["ship"] is None:
                options.append((r, None, None, None))
                continue
            price = r["price"] * rates[r["currency"]]
            ship = r["ship"] * rates[r["ship_cur"]]
            options.append((r, price, ship, price + ship))
        buyable = [o[3] for o in options if o[3] is not None and o[0]["status"] in ("in_stock", "preorder")]
        best = min(buyable) if buyable else None
        for r, price, ship, total in options:
            status = r["status"].replace("_", " ")
            if total is None:
                print(f"   {r['shop']:<10} {status:<10} price unknown")
                continue
            flag = "   <- cheapest delivered" if total == best and len(buyable) > 1 else ""
            print(f"   {r['shop']:<10} {status:<10} A${price:6.2f} + shipping A${ship:5.2f} = A${total:6.2f}{flag}")
    print("\nDelivered totals are for buying ONE record. Shipping is cheaper per record when you buy several.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        init_db()
    elif cmd == "compare":
        compare()
    elif cmd == "all":
        for slug in ADAPTERS:
            run(slug)
    elif cmd in ADAPTERS:
        run(cmd)
    else:
        print(__doc__)
