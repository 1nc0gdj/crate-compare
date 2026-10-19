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
DELAY_SECONDS = 2  # be polite: one request every few seconds


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
    previews: list | None = None      # [{'title': ..., 'url': ...}] direct audio links
    label_url: str | None = None      # the label's own page at this shop
    genres: list | None = None        # the shop's own genre labels, exactly as written there
    artist_urls: dict | None = None   # {artist name: that artist's own page at this shop}


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
        genres = []
        mark = node.find(string=re.compile(r"Add to playlist"))
        if mark:
            cand = mark.find_next(string=lambda x: x.strip() and "Add to playlist" not in x)
            if (cand and len(cand.strip()) <= 45 and not cand.strip().startswith("Review")
                    and not cand.find_all_previous("a", href=re.compile("/MP3/"))):
                genres = [cand.strip()]
        previews = []
        for n, a in enumerate(node.select('a[href*="/MP3/"]'), 1):
            t = a.find_next(string=lambda x: x.strip())   # the track title follows its play link
            name = t.strip() if t else ""
            if not name or len(name) > 80:
                name = f"Track {n}"
            previews.append({"title": name, "url": urljoin(self.BASE, a["href"])})
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
            previews=previews or None,
            label_url=urljoin(self.BASE, label["href"]) if label else None,
            genres=genres or None,
            artist_urls={a.get_text(strip=True): urljoin(self.BASE, a["href"])
                         for a in node.select('a[href*="/artists/"]')
                         if a.get_text(strip=True) and "?" not in a["href"]} or None,
        )


# ---------- Deejay.de adapter ----------
def deejay_audio_url(article, letter):
    """From Deejay's own player: /streamit/<2nd-last digit>/<last digit>/<release number><track letter>.mp3"""
    return f"https://www.deejay.de/streamit/{article[-2]}/{article[-1]}/{article}{letter}.mp3"


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

        genres, main, has_sub = [], None, False
        for a in node.select("a[href]"):
            m = re.search(r"/m_([^/]+)/sm_[^/]+(/substyles_\d+)?$", a["href"])
            name = a.get_text(strip=True)
            if not m or not name:
                continue
            if m.group(2) is None:                       # a main genre
                if main and not has_sub:
                    genres.append(main)
                main, has_sub = name, False
            else:                                        # a sub-style of the current main genre
                genres.append(f"{main} / {name}" if main else name)
                has_sub = True
        if main and not has_sub:
            genres.append(main)
        previews = []
        for a in node.select('a[id^="playTrack_"]'):
            m = re.match(r"playTrack_(\d+)_([a-z0-9]+)$", a.get("id", ""))
            if not m:
                continue
            pos, name = a.find("b"), a.select_one(".trackname")
            label_txt = ((pos.get_text(strip=True) + ": ") if pos else "") + (name.get_text(strip=True) if name else "")
            previews.append({"title": label_txt.strip(": ") or f"Track {len(previews) + 1}",
                             "url": deejay_audio_url(m.group(1), m.group(2))})

        img = node.select_one("img")
        url = urljoin(self.BASE, href)
        return Listing(
            retailer_slug=self.slug, title=title, artists=artists, label=label,
            catalogue_no=norm_cat(cat), format=fmt, price=price, currency="EUR",
            status=status, url=url, preview_url=url,
            artwork_url=img["src"] if img and img.has_attr("src") else None,
            release_date=release_date, previews=previews or None,
            label_url=urljoin(self.BASE, label_el["href"]) if label_el else None,
            genres=genres or None,
            artist_urls={a.get_text(strip=True): urljoin(self.BASE, a["href"])
                         for a in node.select("h2 a[href]") if a.get_text(strip=True)} or None,
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
    deep_keys = None     # set by run(): the followed labels
    deep_artists = None  # set by run(): the followed artists (simple_key form)
    max_deep = None      # set by run(): cap on slow checks per run
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
        unique = {}
        for i in out:                      # a record can show up on a label page and in a feed
            unique.setdefault(i.url, i)
        out = list(unique.values())
        deep = [i for i in out if self.deep_keys is None or name_key(i.label) in self.deep_keys
                or any(simple_key(a) in (self.deep_artists or ()) for a in i.artists)]
        if self.max_deep and len(deep) > self.max_deep:
            print(f"  limiting the slow check to {self.max_deep} of {len(deep)} releases this run")
            deep = deep[:self.max_deep]
        print(f"  checking {len(deep)} of {len(out)} release pages for stock status and tracks "
              f"(only labels and artists you follow get this slower check)...")
        for item in deep:
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

    def fetch_tracks(self, product_id):
        """Yoyaku's own player asks this address for a release's tracks and their mp3 links."""
        time.sleep(DELAY_SECONDS)
        r = requests.get(f"{self.BASE}/wp-json/fwap/v1/track/{product_id}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") or data.get("tracks") or []
        out = []
        for t in rows:
            url = (t.get("mp3") or t.get("url") or "").strip()
            if not url or t.get("playable") is False:
                continue
            name = re.sub(r"^[\s\u2013\u2014-]+", "", t.get("title") or "").strip()
            pos = (t.get("position") or "").strip()
            out.append({"title": f"{pos}: {name}" if pos and name else (name or pos or f"Track {len(out) + 1}"),
                        "url": url})
        return out

    def confirm_status(self, item):
        try:
            soup = self.fetch(item.url)
        except requests.RequestException as e:
            print(f"    could not open {item.url} ({e}); keeping the listing's status")
            return
        play = soup.select_one("a.fwap-play[data-product]")
        if play:
            try:
                item.previews = self.fetch_tracks(play["data-product"]) or None
            except (requests.RequestException, ValueError) as e:
                print(f"    could not load preview tracks for {item.title} ({e})")
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

        styles = []
        for a in node.select('a[href*="/style/"]'):
            name = a.get_text(strip=True)
            if name and name not in styles:
                styles.append(name)

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
            label_url=urljoin(self.BASE, label_el["href"]), genres=styles or None,
            artist_urls={a.get_text(strip=True): urljoin(self.BASE, a["href"])
                         for a in node.select('a[href*="/artist/"]') if a.get_text(strip=True)} or None,
        )


# ---------- Closer adapter (Glenelg, South Australia; a Shopify store) ----------
class CloserAdapter(Adapter):
    """
    Shopify stores publish their whole catalogue as plain data at /products.json, so there is no
    page scraping here. Each product has: title 'Artist - Title', vendor = the label,
    product_type = the format, tags = the genres, price in AUD, and an 'available' flag.
    """
    slug = "closer"
    BASE = "https://closerclosercloser.com"
    SKIP = ("gift", "voucher", "merch", "shirt", "tee", "tote", "slipmat", "cap", "hoodie", "poster", "sticker", "bag")

    def scrape(self, urls):
        out = []
        for url in urls:
            time.sleep(DELAY_SECONDS)
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                products = r.json().get("products", [])
            except (requests.RequestException, ValueError) as e:
                print(f"  {url} -> could not open ({e}); skipping")
                continue
            kept = []
            for p in sorted(products, key=lambda x: x.get("published_at") or "", reverse=True):   # newest first
                item = self.parse(p)
                if item:
                    self.ids[item.url] = p.get("id")
                    kept.append(item)
            print(f"  {url} -> {len(products)} products, {len(kept)} records")
            out += kept
        self.add_previews(out)
        return out

    MAX_PREVIEW_LOOKUPS = 150  # per run (about 5 minutes); records still without previews are picked up on later days

    def __init__(self):
        self.ids = {}            # record address -> Shopify product id
        self.with_player = None  # product ids that have an audio player (fetched once per run)

    def add_previews(self, listings):
        """Closer uses a Shopify audio-player app. Its track list for a product is a public address:
        /apps/awplayer/tracks?product_id=<id>  ->  {'tracks': [{'title': ..., 'url': ...}]}"""
        try:
            if self.with_player is None:
                time.sleep(DELAY_SECONDS)
                r = requests.get(f"{self.BASE}/apps/awplayer/products/ids", headers=HEADERS, timeout=30)
                r.raise_for_status()
                self.with_player = set(r.json().get("product_ids", []))
            have = {row["url"] for row in connect().execute(
                """SELECT li.url FROM listings li JOIN retailers rt ON rt.id = li.retailer_id
                   WHERE rt.slug = 'closer' AND li.previews IS NOT NULL""")}
        except Exception as e:
            print(f"  closer previews: skipped ({e})")
            return
        todo = [L for L in listings if L.url not in have and self.ids.get(L.url) in self.with_player]
        todo.sort(key=lambda L: L.status == "sold_out")   # records you can buy first (the sort keeps newest-first order within each group)
        for L in todo[:self.MAX_PREVIEW_LOOKUPS]:
            time.sleep(DELAY_SECONDS)
            try:
                r = requests.get(f"{self.BASE}/apps/awplayer/tracks", params={"product_id": self.ids[L.url]},
                                 headers=HEADERS, timeout=30)
                r.raise_for_status()
                tracks = r.json().get("tracks", [])
            except (requests.RequestException, ValueError):
                continue
            L.previews = [{"title": t.get("title") or f"Track {n}", "url": t["url"]}
                          for n, t in enumerate(tracks, 1) if t.get("url")] or None
            L.preview_url = L.previews[0]["url"] if L.previews else None
        print(f"  closer previews: {min(len(todo), self.MAX_PREVIEW_LOOKUPS)} looked up, {max(0, len(todo) - self.MAX_PREVIEW_LOOKUPS)} left for later runs")

    @staticmethod
    def find_cat(p):
        """The catalogue number: from the description if it says 'Cat ...', else from the cover image's file name."""
        text = BeautifulSoup(p.get("body_html") or "", "html.parser").get_text(" ", strip=True)
        m = re.search(r"\bcat(?:alogue)?(?:\s*(?:no|number|#))?\.?\s*[:#\-]?\s*([A-Z]{2,10}[ \-]?\d{1,4})\b", text, re.I)
        if m:
            return norm_cat(m.group(1))
        m = re.search(r"\(([A-Z]{2,10}[ \-]?\d{1,4})\)", text[:400])     # e.g. 'Junction Forest (SHROOM12)'
        if m:
            return norm_cat(m.group(1))
        for img in p.get("images") or []:
            stem = (img.get("src") or "").split("?")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            m = re.match(r"([A-Za-z]{2,10}[-_ ]?\d{1,4})(?:[-_ ]?(?:cover|front|back|sleeve|a|b))?$", stem, re.I)
            if m:
                return norm_cat(m.group(1))
        m = re.search(r"-([a-z]{2,8}-?\d{2,4})$", p.get("handle") or "")      # some addresses end in the catalogue number
        return norm_cat(m.group(1)) if m else None

    def parse(self, p):
        ptype = (p.get("product_type") or "").strip()
        low = ptype.lower()
        if not low or any(w in low.split() or w in low for w in self.SKIP):
            return None
        if not (re.search(r"vinyl|lp|\b(12|10|7)\s*(\"|inch|in)", low) or re.search(r"\b(12|10|7)\"", ptype)):
            return None
        title = (p.get("title") or "").strip()
        if " - " in title:
            artist_part, name = title.split(" - ", 1)
        else:
            artist_part, name = "", title
        artists = [a.strip() for a in re.split(r",\s+|\s+\+\s+", artist_part) if a.strip()]
        variants = p.get("variants") or [{}]
        v = next((x for x in variants if x.get("available")), variants[0])
        tags = [t.strip() for t in (p.get("tags") or []) if t.strip()]
        tagtext = " ".join(tags).lower() + " " + low + " " + title.lower()
        if not v.get("available"):
            status = "sold_out"
        elif "pre-order" in tagtext or "preorder" in tagtext or "pre order" in tagtext:
            status = "preorder"
        else:
            status = "in_stock"
        try:
            price = float(v.get("price"))
        except (TypeError, ValueError):
            price = None
        imgs = p.get("images") or []
        return Listing(
            retailer_slug=self.slug,
            title=name or title,
            artists=artists,
            label=(p.get("vendor") or "").strip(),
            catalogue_no=self.find_cat(p),
            format=ptype,
            price=price,
            currency="AUD",
            status=status,
            url=f"{self.BASE}/products/{p.get('handle')}",
            artwork_url=imgs[0].get("src") if imgs else None,
            release_date=(p.get("published_at") or "")[:10] or None,   # when the shop listed it
            genres=[t for t in tags if not re.search(r"pre-?order|new|sale|vinyl", t, re.I)] or None,
        )


ADAPTERS = {"juno": JunoAdapter, "deejay": DeejayAdapter, "yoyaku": YoyakuAdapter, "closer": CloserAdapter}

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
    # Closer (AUD): flat rate to Sydney; the shop also says orders over A$180 ship free (not used in the planner yet).
    "closer": {"first": 17.00, "extra": 0.00},   # AUD flat rate: 1, 2 or 3 records are all A$17 (checked by Jamie, 19 Oct 2026)
}


# ---------- DB helpers ----------
def connect():
    _KEYS.clear()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect()
    con.executescript(Path("schema.sql").read_text())
    have = [r["name"] for r in con.execute("PRAGMA table_info(listings)")]
    for col in ("previews", "genres"):
        if col not in have:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")   # older databases
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
    con.execute(
        """INSERT OR IGNORE INTO retailers
           (slug, name, base_url, currency, ship_first_item, ship_extra_item, free_ship_over)
           VALUES ('closer','Closer','https://closerclosercloser.com','AUD', NULL, NULL, 180)"""
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


_KEYS = {}   # remembers name -> id for the current run, so thousands of listings stay fast


def simple_key(name: str) -> str:
    return re.sub(r"[^\w]+", "", name.lower())


def get_or_create(con, table, name):
    keyfn = name_key if table == "labels" else simple_key   # labels match loosely; artists must match exactly
    if table not in _KEYS:
        _KEYS[table] = {keyfn(r["name"]): r["id"] for r in con.execute(f"SELECT id, name FROM {table}")}
    key = keyfn(name)
    if key not in _KEYS[table]:
        _KEYS[table][key] = con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid
    return _KEYS[table][key]


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

    if L.label_url and label_id:
        con.execute("INSERT OR IGNORE INTO label_pages (label_id, retailer_id, url) VALUES (?,?,?)",
                    (label_id, retailer_id, L.label_url))

    for aname, aurl in (L.artist_urls or {}).items():      # remember each artist's own page here
        aid = get_or_create(con, "artists", aname)
        con.execute("INSERT OR IGNORE INTO artist_pages (artist_id, retailer_id, url) VALUES (?,?,?)",
                    (aid, retailer_id, aurl))

    # 2) Upsert this retailer's listing.
    con.execute(
        """INSERT INTO listings (release_id, retailer_id, url, price, currency, status, preview_url, previews, genres)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(release_id, retailer_id) DO UPDATE SET
             url=excluded.url, price=excluded.price, currency=excluded.currency,
             status=excluded.status, preview_url=excluded.preview_url,
             previews=COALESCE(excluded.previews, previews), genres=COALESCE(excluded.genres, genres),
             last_seen=datetime('now')""",
        (release_id, retailer_id, L.url, L.price, L.currency, L.status, L.preview_url,
         json.dumps(L.previews) if L.previews else None, json.dumps(L.genres) if L.genres else None),
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


MAX_LABEL_PAGES = 6     # at most this many pages per followed label per shop
MAX_DEEP_CHECKS = 150   # cap on the slow Yoyaku per-release checks in one run


def load_config():
    p = Path("config.json")
    return json.loads(p.read_text()) if p.exists() else {}


def fetch_followed_keys():
    """What anyone follows on the site (names and counts only), from the Supabase database.
    Returns (label keys, artist keys); artists are stored with an 'a:' prefix."""
    cfg = load_config()
    if not cfg.get("supabase_url") or not cfg.get("supabase_key"):
        return set(), set()
    try:
        r = requests.post(
            cfg["supabase_url"].rstrip("/") + "/rest/v1/rpc/followed_labels",
            headers={"apikey": cfg["supabase_key"], "Content-Type": "application/json"},
            data="{}", timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  could not read follows from Supabase ({e}); carrying on without them")
        return set(), set()
    keys = [row["lkey"] for row in rows if row.get("lkey")]
    labels = {k for k in keys if not k.startswith("a:")}
    artists = {k[2:] for k in keys if k.startswith("a:")}
    print(f"  {len(labels)} labels and {len(artists)} artists are followed on the site")
    return labels, artists


def followed_artist_pages(slug, keys):
    """This shop's page for each followed artist, taken from pages we've already seen listed."""
    if not keys:
        return []
    con = connect()
    rows = con.execute(
        """SELECT a.name, ap.url FROM artist_pages ap
           JOIN artists a ON a.id = ap.artist_id
           JOIN retailers rt ON rt.id = ap.retailer_id WHERE rt.slug = ?""", (slug,)).fetchall()
    return [r["url"] for r in rows if simple_key(r["name"]) in keys]


def followed_label_pages(slug, keys):
    """This shop's page for each followed label, taken from pages we've already seen listed."""
    if not keys:
        return []
    con = connect()
    rows = con.execute(
        """SELECT lb.name, lp.url FROM label_pages lp
           JOIN labels lb ON lb.id = lp.label_id
           JOIN retailers rt ON rt.id = lp.retailer_id WHERE rt.slug = ?""", (slug,)).fetchall()
    return [r["url"] for r in rows if name_key(r["name"]) in keys]


def manual_label_pages(slug):
    """label_pages.json: label page addresses you paste in by hand, e.g.
    {"Altered Circuits": {"juno": "https://www.juno.co.uk/labels/Altered+Circuits+Belgium/"}}"""
    p = Path("label_pages.json")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        print(f"  label_pages.json isn't valid ({e}); ignoring it")
        return []
    return [v[slug] for v in data.values() if isinstance(v, dict) and v.get(slug)]


def labels_without_page(slug, keys, manual_urls):
    """Names of followed labels this shop has no page for yet."""
    con = connect()
    have = {name_key(r["name"]) for r in con.execute(
        """SELECT lb.name FROM label_pages lp JOIN labels lb ON lb.id = lp.label_id
           JOIN retailers rt ON rt.id = lp.retailer_id WHERE rt.slug = ?""", (slug,))}
    names = {}
    for r in con.execute("SELECT name FROM labels"):
        k = name_key(r["name"])
        if k in keys and k not in have:
            names.setdefault(k, r["name"])
    return list(names.values())


SEARCH_URLS = {"juno": "https://www.juno.co.uk/search/?q%5Ball%5D%5B%5D={q}&solrorder=relevancy&show_out_of_stock=1",
               "yoyaku": "https://yoyaku.io/?s={q}&post_type=product"}
MAX_DISCOVER = 12    # label searches per shop per run


def discover_label_pages(slug, adapter, names, started):
    """For followed labels this shop has no page for, search the shop for the label's name.
    Releases found for exactly that label are kept, and the label's page address is remembered
    (the next step then fetches its full catalogue)."""
    if slug not in SEARCH_URLS or not names:
        return [], []
    found_listings, found_pages = [], []
    for name in names[:MAX_DISCOVER]:
        if time.time() - started > SHOP_BUDGET_SECONDS:
            break
        want = name_key(name)
        url = SEARCH_URLS[slug].format(q=requests.utils.quote(name))
        try:
            hits = [L for L in adapter.scrape([url]) if L.label and name_key(L.label) == want]
        except Exception as e:                 # a search that fails must never stop the run
            print(f"  looking for {name} at {slug}: failed ({e})")
            continue
        pages = {L.label_url for L in hits if L.label_url}
        print(f"  looking for {name} at {slug}: {len(hits)} releases, label page {'found' if pages else 'not found'}")
        found_listings += hits
        found_pages += sorted(pages)
    return found_listings, found_pages


def fetch_requested_names():
    """Artists and labels people asked for on the site (names only), from the Supabase database."""
    cfg = load_config()
    if not cfg.get("supabase_url") or not cfg.get("supabase_key"):
        return []
    try:
        r = requests.post(cfg["supabase_url"].rstrip("/") + "/rest/v1/rpc/requested_names",
                          headers={"apikey": cfg["supabase_key"], "Content-Type": "application/json"},
                          data="{}", timeout=30)
        r.raise_for_status()
        rows = [(x["kind"], x["name"]) for x in r.json() if x.get("name")]
    except Exception as e:
        print(f"  could not read requests from Supabase ({e}); carrying on without them")
        return []
    print(f"  {len(rows)} artists/labels have been requested on the site")
    return rows


def discover_requested(slug, adapter, wanted, started):
    """Search this shop for each requested artist or label. Keeps releases that really are by that
    artist or on that label, and remembers their page addresses so the full catalogue gets fetched."""
    if slug not in SEARCH_URLS or not wanted:
        return [], []
    found, pages = [], []
    for kind, name in wanted[:MAX_DISCOVER * 2]:
        if time.time() - started > SHOP_BUDGET_SECONDS:
            break
        url = SEARCH_URLS[slug].format(q=requests.utils.quote(name))
        try:
            results = adapter.scrape([url])
        except Exception as e:
            print(f"  looking for {kind} {name} at {slug}: failed ({e})")
            continue
        if kind == "label":
            hits = [L for L in results if L.label and name_key(L.label) == name_key(name)]
            urls = {L.label_url for L in hits if L.label_url}
        else:
            want = simple_key(name)
            hits = [L for L in results if want in {simple_key(a) for a in L.artists}]
            urls = {u for L in hits for a, u in (L.artist_urls or {}).items() if simple_key(a) == want}
        print(f"  looking for {kind} {name} at {slug}: {len(hits)} releases, page {'found' if urls else 'not found'}")
        found += hits
        pages += sorted(urls)
    return found, pages


def load_feeds():
    p = Path("feeds.json")
    return json.loads(p.read_text()) if p.exists() else {}


def page_url(slug, base, n):
    """The address of page n of a shop's listing."""
    if n == 1:
        return base
    if slug == "juno":
        path, _, query = base.partition("?")
        return path.rstrip("/") + f"/{n}/" + (f"?{query}" if query else "")
    if slug == "deejay":
        return re.sub(r"/page_\d+$", f"/page_{n}", base)
    if slug == "yoyaku":
        return base.rstrip("/") + f"/page/{n}/"
    if slug == "closer":
        return base + ("&" if "?" in base else "?") + f"page={n}"
    return base


def expand_feed(slug):
    cfg = load_feeds().get(slug) or {}
    return [page_url(slug, base, n) for base in cfg.get("urls", []) for n in range(1, int(cfg.get("pages", 1)) + 1)]


FULL_PAGE = 20   # a page with at least this many records may continue on a next page
SHOP_BUDGET_SECONDS = 14 * 60  # most time one shop may spend on extra label/artist pages; whatever is done by then is saved


def run(slug):
    started = time.time()
    follows = load_follows()
    if follows:
        urls = []
        for f in follows:
            if f.get(slug):
                urls.append(f[slug])
            else:
                print(f"  {f['name']}: no {slug} page in follows.json yet; skipping")
    else:  # no follow list: fall back to the generic pages above
        urls = {"juno": JUNO_URLS, "deejay": DEEJAY_URLS, "yoyaku": YOYAKU_URLS, "closer": []}[slug]
    urls += [u for u in expand_feed(slug) if u not in urls]
    if not urls:
        print(f"{slug}: nothing to scrape.")
        return
    cloud_keys, artist_keys = fetch_followed_keys()
    label_pages = [u for u in followed_label_pages(slug, cloud_keys) if u not in urls]
    label_pages += [u for u in followed_artist_pages(slug, artist_keys) if u not in urls and u not in label_pages]
    adapter = ADAPTERS[slug]()
    label_pages += [u for u in manual_label_pages(slug) if u not in urls and u not in label_pages]
    if slug == "yoyaku" and follows:
        adapter.deep_keys = {name_key(f["name"]) for f in follows} | cloud_keys
        adapter.deep_artists = artist_keys
        adapter.max_deep = MAX_DEEP_CHECKS
    scraped = adapter.scrape(urls)
    have = {L.url for L in scraped}
    missing = labels_without_page(slug, cloud_keys | {name_key(f["name"]) for f in follows}, label_pages)
    extra, new_pages = discover_label_pages(slug, adapter, missing, started)
    scraped += [L for L in extra if L.url not in have]
    have.update(L.url for L in extra)
    label_pages += [u for u in new_pages if u not in urls and u not in label_pages]
    asked, asked_pages = discover_requested(slug, adapter, fetch_requested_names(), started)
    scraped += [L for L in asked if L.url not in have]
    have.update(L.url for L in asked)
    label_pages += [u for u in asked_pages if u not in urls and u not in label_pages]
    if slug == "juno":                      # ask Juno to include sold-out records too, so they show as sold out instead of missing
        label_pages = [u if "show_out_of_stock" in u else u + ("&" if "?" in u else "?") + "show_out_of_stock=1" for u in label_pages]
    if label_pages:                         # start somewhere different each day so pages skipped for time get their turn
        k = date.today().toordinal() % len(label_pages)
        label_pages = label_pages[k:] + label_pages[:k]
    # Pass 1: the first page of every followed label/artist, so nothing is skipped just because the run was slow.
    # Pass 2: further pages, only for pages that came back full.
    out_of_time, full_pages = False, []
    for i, base in enumerate(label_pages):
        if time.time() - started > SHOP_BUDGET_SECONDS:
            print(f"  {slug}: time budget used up; skipping the remaining {len(label_pages) - i} label/artist pages until tomorrow's run")
            out_of_time = True
            break
        results = adapter.scrape([base])
        if len(results) >= FULL_PAGE:
            full_pages.append(base)
        fresh = [L for L in results if L.url not in have]
        have.update(L.url for L in fresh)
        scraped += fresh
    for base in full_pages:
        for n in range(2, MAX_LABEL_PAGES + 1):
            if time.time() - started > SHOP_BUDGET_SECONDS:
                break
            if page_url(slug, base, n) == base:     # this shop's paging isn't known here: don't fetch page 1 twice
                break
            fresh = [L for L in adapter.scrape([page_url(slug, base, n)]) if L.url not in have]
            if not fresh:
                break
            have.update(L.url for L in fresh)
            scraped += fresh
    listings, seen = [], set()
    for L in scraped:                       # the same record can appear in several feeds
        if L.url not in seen:
            seen.add(L.url)
            listings.append(L)
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


def audio_check():
    """Check that preview audio can be loaded the way our web page would load it (from another site)."""
    as_our_site = {**HEADERS, "Referer": "https://example.github.io/crate-compare/", "Range": "bytes=0-1023"}

    def try_audio(name, u):
        try:
            r = requests.get(u, headers=as_our_site, timeout=30)
            print(f"  {name}: HTTP {r.status_code}, {r.headers.get('Content-Type')}, "
                  f"{r.headers.get('Content-Range') or r.headers.get('Content-Length')} bytes  <- {u}")
        except requests.RequestException as e:
            print(f"  {name}: error {e}  <- {u}")
        time.sleep(DELAY_SECONDS)

    print("1. Can an audio file be loaded by a page on another website?")
    try_audio("Juno", "https://www.juno.co.uk/MP3/SF1183207-01-01-01.mp3")
    try_audio("Deejay.de", deejay_audio_url("1092733", "a"))

    print("\n2. Yoyaku's track list for the Ikigai release (product 752882):")
    u = "https://yoyaku.io/wp-json/fwap/v1/track/752882"
    try:
        r = requests.get(u, headers=HEADERS, timeout=30)
        print(f"  HTTP {r.status_code} <- {u}")
        print("  ", r.text[:1500])
        m = re.search(r"https?:[^\"'\s<>]+?\.(?:mp3|m4a|ogg|wav)[^\"'\s<>]*", r.text.replace("\\/", "/"))
        if m:
            print("\n3. Loading the first Yoyaku audio file from another website:")
            try_audio("Yoyaku", m.group(0))
    except requests.RequestException as e:
        print("  error:", e)


def norm_genre(s):
    """'Minimal / Tech House' and 'Minimal/Tech House' count as the same label."""
    return re.sub(r"\s+", " ", re.sub(r"\s*/\s*", "/", s.strip().lower()))


def load_genre_map():
    """genres.json: the simplified genre list, and which shop labels belong to which."""
    p = Path("genres.json")
    cfg = json.loads(p.read_text()) if p.exists() else {"genres": [], "map": {}}
    return cfg, {k.split(":", 1)[0].lower() + ":" + norm_genre(k.split(":", 1)[1]): v for k, v in cfg["map"].items()}


def simplified_genres(shop, raw_labels, gmap):
    """The simplified genres for one listing's raw labels (empty if none are mapped yet)."""
    out = []
    for raw in raw_labels or []:
        for g in gmap.get(f"{shop}:{norm_genre(raw)}", []):
            if g not in out:
                out.append(g)
    return out


def genre_report(only_unmapped=True):
    """Count every raw genre label per shop, and say which ones genres.json doesn't cover yet."""
    cfg, gmap = load_genre_map()
    con = connect()
    rows = con.execute("""SELECT rt.slug AS shop, li.genres FROM listings li
                          JOIN retailers rt ON rt.id = li.retailer_id WHERE li.genres IS NOT NULL""").fetchall()
    counts, total, covered = {}, len(rows), 0
    for r in rows:
        raws = json.loads(r["genres"])
        if simplified_genres(r["shop"], raws, gmap):
            covered += 1
        for raw in raws:
            counts[(r["shop"], raw)] = counts.get((r["shop"], raw), 0) + 1
    all_listings = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"\nGenres: {total} of {all_listings} listings have shop genre labels; {covered} have at least one mapped to your simplified list.")
    unmapped = {k: n for k, n in counts.items() if f"{k[0]}:{norm_genre(k[1])}" not in gmap}
    show = unmapped if only_unmapped else counts
    print(f"{'Labels not in genres.json yet' if only_unmapped else 'All shop genre labels'} ({len(show)}):")
    for (shop, raw), n in sorted(show.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
        mapped = gmap.get(f"{shop}:{norm_genre(raw)}")
        print(f"  {shop:<7} {raw:<34} x{n:<4}" + (f" -> {', '.join(mapped)}" if mapped else ""))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        init_db()
    elif cmd == "audio-check":
        audio_check()
    elif cmd == "compare":
        compare()
    elif cmd == "genres":
        genre_report(only_unmapped=False)
    elif cmd == "all":
        for slug in ADAPTERS:
            run(slug)
        genre_report()
    elif cmd in ADAPTERS:
        run(cmd)
    else:
        print(__doc__)
