"""
Build site.html: a page of releases from the labels you follow, with every shop's
price delivered to Sydney in AUD.

Usage:   python build_site.py      then open site.html in your browser.
Needs scraper.py in the same folder (it reuses its database and exchange rates).
"""
import json
from datetime import date
from pathlib import Path

import scraper

AVAILABLE = ("in_stock", "preorder")


def display_name(name):
    """'Delmark US' -> 'Delmark': drop a trailing country word the shops add to label names."""
    words = name.split()
    while len(words) > 1 and words[-1].lower() in scraper.COUNTRY_WORDS:
        words.pop()
    return " ".join(words)


def build():
    rates = scraper.get_rates()
    con = scraper.connect()
    rows = con.execute(
        """SELECT r.id, r.title, COALESCE(lb.name, '') AS label, r.catalogue_no, r.format,
                  r.release_date, r.artwork_url,
                  (SELECT group_concat(a.name, char(31)) FROM release_artists ra
                     JOIN artists a ON a.id = ra.artist_id WHERE ra.release_id = r.id) AS artists,
                  rt.name AS shop, rt.slug AS slug, rt.currency AS ship_cur, rt.ship_first_item AS ship,
                  li.price, li.currency, li.status, li.url, li.preview_url, li.first_seen, li.previews, li.genres
           FROM releases r
           LEFT JOIN labels lb ON lb.id = r.label_id
           JOIN listings li ON li.release_id = r.id
           JOIN retailers rt ON rt.id = li.retailer_id
           WHERE r.label_id IS NOT NULL
           ORDER BY r.id"""
    ).fetchall()

    gcfg, gmap = scraper.load_genre_map()
    releases = {}
    for r in rows:
        rel = releases.setdefault(r["id"], {
            "title": r["title"], "artists": ", ".join((r["artists"] or "").split("\x1f")) if r["artists"] else "",
            "akeys": sorted({"a:" + scraper.simple_key(n) for n in (r["artists"] or "").split("\x1f") if scraper.simple_key(n)} - {"a:various", "a:variousartists"}),
            "anames": [n for n in (r["artists"] or "").split("\x1f") if n], "label": r["label"], "lkey": scraper.name_key(r["label"]),
            "cat": r["catalogue_no"] or "", "format": r["format"] or "",
            "date": r["release_date"] or "", "art": r["artwork_url"] or "", "added": "", "offers": [], "genres": [],
        })
        rel["added"] = min(rel["added"] or r["first_seen"], r["first_seen"])
        if r["genres"]:
            for g in scraper.simplified_genres(r["slug"], json.loads(r["genres"]), gmap):
                if g not in rel["genres"]:
                    rel["genres"].append(g)
        price = ship = total = None
        if r["price"] is not None and r["ship"] is not None:
            price = round(r["price"] * rates[r["currency"]], 2)
            ship = round(r["ship"] * rates[r["ship_cur"]], 2)
            total = round(price + ship, 2)
        rel["offers"].append({
            "shop": r["shop"], "status": r["status"], "price": price, "ship": ship,
            "total": total, "url": r["url"], "preview": r["preview_url"] or "",
            "tracks": json.loads(r["previews"]) if r["previews"] else [],
        })

    out = []
    for rel in releases.values():
        buyable = [o for o in rel["offers"] if o["status"] in AVAILABLE and o["total"] is not None]
        rel["best"] = min(buyable, key=lambda o: o["total"])["shop"] if len(buyable) > 1 else ""
        rel["offers"].sort(key=lambda o: (o["status"] not in AVAILABLE, o["total"] or 1e9))
        out.append(rel)
    out.sort(key=lambda x: x["date"], reverse=True)

    # Labels: one entry each, with a clean name, a release count, and whether you track its full catalogue
    seed = {scraper.name_key(f["name"]): f["name"] for f in scraper.load_follows()}
    names, counts = {}, {}
    for rel in out:
        k = rel["lkey"]
        counts[k] = counts.get(k, 0) + 1
        names.setdefault(k, seed.get(k) or display_name(rel["label"]))
    for rel in out:
        rel["label"] = names[rel["lkey"]]
    labels = sorted(({"key": k, "name": names[k], "n": counts[k], "full": k in seed} for k in counts),
                    key=lambda l: (-l["n"], l["name"].lower()))

    # Artists: one entry each, with a release count (the same name written two ways counts as one)
    art = {}
    for rel in out:
        for k, n in zip([("a:" + scraper.simple_key(n)) for n in rel["anames"]], rel["anames"]):
            if k in rel["akeys"]:
                e = art.setdefault(k, {"key": k, "name": n, "n": 0})
                e["n"] += 1
    artists = sorted(art.values(), key=lambda a: (-a["n"], a["name"].lower()))
    for rel in out:       # [name, key] pairs, so the page can link each artist's name to their page
        rel["anames"] = [[n, ("a:" + scraper.simple_key(n)) if ("a:" + scraper.simple_key(n)) in rel["akeys"] else ""] for n in rel["anames"]]

    # each label's and artist's own page at each shop
    for l in labels: l["pages"] = {}
    for a in artists: a["pages"] = {}
    lby, aby = {l["key"]: l for l in labels}, {a["key"]: a for a in artists}
    for r in con.execute("SELECT lb.name AS name, rt.name AS shop, lp.url FROM label_pages lp JOIN labels lb ON lb.id = lp.label_id JOIN retailers rt ON rt.id = lp.retailer_id"):
        if scraper.name_key(r["name"]) in lby: lby[scraper.name_key(r["name"])]["pages"][r["shop"]] = r["url"]
    for r in con.execute("SELECT a.name AS name, rt.name AS shop, ap.url FROM artist_pages ap JOIN artists a ON a.id = ap.artist_id JOIN retailers rt ON rt.id = ap.retailer_id"):
        if "a:" + scraper.simple_key(r["name"]) in aby: aby["a:" + scraper.simple_key(r["name"])]["pages"][r["shop"]] = r["url"]

    shops = {r["name"]: {"first": round(r["ship_first_item"] * rates[r["currency"]], 2),
                         "extra": round(r["ship_extra_item"] * rates[r["currency"]], 2)}
             for r in con.execute("SELECT name, currency, ship_first_item, ship_extra_item FROM retailers")
             if r["name"] in {o["shop"] for rel in out for o in rel["offers"]}}

    cfg = scraper.load_config()
    data = {
        "supabase": ({"url": cfg["supabase_url"].rstrip("/"), "key": cfg["supabase_key"]}
                     if cfg.get("supabase_url") and cfg.get("supabase_key") else None),
        "labels": labels, "artists": artists, "shops": shops, "defaults": list(seed), "genreOrder": gcfg.get("genres", []),
        "generated": date.today().isoformat(),
        "gbp": round(rates["GBP"], 2), "eur": round(rates["EUR"], 2),
        "releases": out,
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    Path("site.html").write_text(html, encoding="utf-8")
    print(f"Built site.html with {len(out)} releases. Open it in your browser.")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New from your labels</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap">
<style>
:root{
  --fog:#E9EEEB; --ink:#15262C; --paper:#FFFFFF; --dim:#526166;
  --sticker:#F2B01E; --teal:#16695C; --rule:#B8C4C1;
  --font:"Archivo",system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--fog);color:var(--ink);font-family:var(--font);line-height:1.4}
a{color:inherit}
a:focus-visible,button:focus-visible,select:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
header{padding:1.2rem clamp(1rem,4vw,3rem) 1rem}
.headrow{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
#authBtn{flex:none;white-space:nowrap}
.eacts{display:flex;gap:.6rem;flex-wrap:wrap;justify-content:flex-end}
#entityview{padding:0 clamp(1rem,4vw,3rem) 3rem}
#entityview .mhead,#entityview .mnote{padding-left:0;padding-right:0}
.entlink{color:inherit;text-decoration:underline;text-decoration-color:var(--rule);text-underline-offset:.15em}
.entlink:hover{text-decoration-color:var(--ink)}
.pextra{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:.3rem 1.4rem;align-items:center;font-size:.8rem}
.pextra label{display:flex;align-items:center;gap:.5rem}
.pextra input[type=range]{width:7.5rem;accent-color:var(--sticker)}
.pextra output{min-width:3.2rem;font-variant-numeric:tabular-nums}
.pextra button{border:1px solid rgba(255,255,255,.5);padding:.15rem .6rem;font-size:.8rem}
h1{margin:0;font-size:clamp(2rem,5.5vw,3.8rem);font-weight:800;font-stretch:125%;line-height:1;letter-spacing:-.02em;max-width:16ch}
.meta{margin:.9rem 0 0;color:var(--dim);max-width:62ch}
.bar{position:sticky;top:0;z-index:5;background:var(--fog);border-bottom:1px solid var(--rule);
  display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;align-items:center;padding:.6rem clamp(1rem,4vw,3rem)}
.tabs{display:flex;gap:1.2rem}
.tabs button{font:inherit;font-weight:600;background:none;border:0;border-bottom:3px solid transparent;padding:.35rem 0;color:var(--dim);cursor:pointer}
.tabs button[aria-pressed=true]{color:var(--ink);border-bottom-color:var(--ink)}
.bar label{display:flex;align-items:center;gap:.5rem;color:var(--dim)}
select{font:inherit;padding:.3rem .5rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink)}
#count{margin-left:auto;color:var(--dim)}
.controls{display:flex;flex-wrap:wrap;gap:.5rem 1.2rem;align-items:center;width:100%}
input[type=search]{font:inherit;padding:.3rem .6rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink);flex:1 1 16rem;max-width:26rem}
details.multi{position:relative}
details.multi summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:.8rem;
  min-width:10rem;max-width:16rem;padding:.3rem .5rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink)}
details.multi summary::-webkit-details-marker{display:none}
details.multi summary::after{content:"";flex:none;width:.4rem;height:.4rem;border:solid var(--ink);border-width:0 2px 2px 0;transform:rotate(45deg);margin-top:-.2rem}
details.multi[open] summary::after{transform:rotate(225deg);margin-top:.2rem}
.panel{position:absolute;z-index:10;top:calc(100% + 4px);left:0;min-width:17rem;max-height:22rem;overflow:auto;background:var(--paper);border:1px solid var(--ink);padding:.4rem .8rem}
.bar .panel label{display:flex;gap:.6rem;padding:.3rem 0;color:var(--ink);cursor:pointer}
.clearbtn{font:inherit;font-weight:600;color:var(--teal);background:none;border:0;border-top:1px solid var(--rule);width:100%;text-align:left;padding:.55rem 0 .25rem;margin-top:.3rem;cursor:pointer}
.check input{accent-color:var(--teal);width:1.05rem;height:1.05rem}
.newtag{position:absolute;left:0;top:0;background:var(--ink);color:var(--paper);padding:.2rem .6rem;font-weight:700;font-size:.85rem}
@media (max-width:700px){.bar{position:static}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:2.4rem 1.6rem;padding:1.8rem clamp(1rem,4vw,3rem) 3rem}
.sleeve{display:flex;flex-direction:column;min-width:0}
.art{position:relative;aspect-ratio:1/1;background:var(--paper);border:1px solid var(--ink)}
.art img{display:block;width:100%;height:100%;object-fit:cover}
.noart{display:grid;place-items:center;height:100%;padding:1rem;text-align:center;font-weight:700;font-stretch:110%;color:var(--dim);background:repeating-linear-gradient(135deg,var(--paper) 0 12px,var(--fog) 12px 24px)}
.sticker{position:absolute;right:-12px;top:-14px;width:5.4rem;height:5.4rem;border-radius:50%;background:var(--sticker);color:var(--ink);
  display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;transform:rotate(9deg);font-size:.78rem;line-height:1.15;font-weight:600;border:1px solid var(--ink)}
.sticker b{font-size:.95rem;font-weight:800}
.title{margin:.8rem 0 0;font-size:1.15rem;font-weight:750;font-stretch:108%;line-height:1.2}
.by{margin:.15rem 0 0;font-weight:500}
.info{margin:.2rem 0 .7rem;color:var(--dim);font-size:.88rem}
.offers{list-style:none;margin:0;padding:0;border-top:1px solid var(--ink)}
.offer{display:flex;justify-content:space-between;gap:.8rem;padding:.55rem 0;border-bottom:1px solid var(--rule)}
.shop{font-weight:700}
.status{display:block;font-size:.82rem;font-weight:600}
.ok{color:var(--teal)} .pre{color:#8A5A00} .out,.unk{color:var(--dim)}
.cost{text-align:right}
.cost strong{display:block;font-size:1.15rem;font-weight:800;font-stretch:110%}
.cost small{color:var(--dim);font-size:.78rem}
.offer.gone .cost strong{color:var(--dim);font-weight:600}
.listen{font-size:.82rem;color:var(--dim)}
.empty{padding:3rem clamp(1rem,4vw,3rem);color:var(--dim)}
.playbtn{position:absolute;left:.6rem;bottom:.6rem;width:3rem;height:3rem;border-radius:50%;border:0;background:var(--ink);color:var(--paper);display:grid;place-items:center;cursor:pointer}
.playbtn svg,.player button svg{width:1.4rem;height:1.4rem;fill:currentColor}
.sleeve.playing .art{outline:3px solid var(--sticker);outline-offset:3px}
.tracks{margin:0 0 .7rem;font-size:.9rem}
.tracks summary{cursor:pointer;color:var(--dim);font-weight:600}
.tracks ol{list-style:none;margin:.3rem 0 0;padding:0}
.tracks button{font:inherit;background:none;border:0;text-align:left;padding:.2rem 0;cursor:pointer;color:var(--ink)}
.tracks button.on{font-weight:800}
.tracks .src{margin:.2rem 0 0;color:var(--dim);font-size:.8rem}
button.listen{font:inherit;font-size:.82rem;background:none;border:0;padding:0;color:var(--teal);font-weight:600;cursor:pointer;text-align:left}
.player{position:fixed;left:0;right:0;bottom:0;z-index:20;background:var(--ink);color:var(--paper);display:grid;
  grid-template-columns:3.2rem minmax(0,1fr) auto minmax(9rem,22rem) auto;gap:.4rem 1rem;align-items:center;
  padding:.6rem clamp(1rem,4vw,3rem) calc(.6rem + env(safe-area-inset-bottom,0px))}
.player[hidden]{display:none}
.player img{width:3.2rem;height:3.2rem;object-fit:cover;background:var(--paper)}
.player img[hidden]{display:block;visibility:hidden}
.ptrack{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.psub{font-size:.82rem;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pctl{display:flex;gap:.3rem}
.player button{font:inherit;background:none;border:0;color:var(--paper);cursor:pointer;padding:.4rem;display:grid;place-items:center}
.player button:focus-visible,.player input:focus-visible{outline:3px solid var(--sticker);outline-offset:2px}
.pseek{display:flex;align-items:center;gap:.5rem;font-size:.8rem;font-variant-numeric:tabular-nums}
.pseek input{flex:1;min-width:0;accent-color:var(--sticker)}
body.has-player{padding-bottom:8rem}
@media (max-width:700px){.player{grid-template-columns:3.2rem minmax(0,1fr) auto auto}.pseek{grid-column:1/-1}body.has-player{padding-bottom:12rem}}
button.manage{font:inherit;padding:.3rem .7rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink);cursor:pointer;font-weight:600}
.savedacts{display:grid;gap:.4rem;margin:.6rem 0 0}
.basketbtn.alt{background:var(--paper);color:var(--teal);border-color:var(--teal)}
.basketbtn.alt[aria-pressed=true]{background:var(--teal);color:var(--paper)}
#wishview{padding:0 clamp(1rem,4vw,3rem) 3rem}
#wishview .mhead,#wishview .mnote{padding-left:0;padding-right:0}
.basketbtn{font:inherit;font-weight:700;margin:0;padding:.35rem .8rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink);cursor:pointer;width:100%}
.basketbtn[aria-pressed=true]{background:var(--ink);color:var(--paper)}
.basketbtn:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
#basketview{padding:0 clamp(1rem,4vw,3rem) 3rem;max-width:56rem}
#basketview .mhead{padding-left:0;padding-right:0}
.bsum{margin:.2rem 0 1rem;padding:1rem 1.2rem;border:2px solid var(--ink);background:var(--paper)}
.bsum strong{font-size:1.5rem;font-stretch:115%}
.bshop{margin:0 0 1.2rem;border:1px solid var(--ink);background:var(--paper)}
.bshop h3{margin:0;padding:.6rem 1rem;background:var(--ink);color:var(--paper);display:flex;justify-content:space-between;gap:1rem;font-size:1rem}
.bshop ul{list-style:none;margin:0;padding:0 1rem}
.bshop li{display:flex;justify-content:space-between;gap:1rem;align-items:baseline;padding:.6rem 0;border-bottom:1px solid var(--rule)}
.bshop li .bt{font-weight:700;display:block}
.bshop li .bm{display:block;color:var(--dim);font-size:.82rem}
.bshop .bfoot{padding:.6rem 1rem;display:flex;justify-content:space-between;gap:1rem;font-weight:700}
.bactions{display:flex;gap:.8rem;flex-wrap:wrap;margin:1rem 0}
.rmbtn{font:inherit;font-size:.82rem;background:none;border:0;padding:0;color:var(--teal);font-weight:600;cursor:pointer}
nav.site{display:flex;gap:1.4rem;padding:.2rem clamp(1rem,4vw,3rem) .6rem;border-bottom:1px solid var(--rule);flex-wrap:wrap}
nav.site a{color:var(--ink);font-weight:700;text-decoration:none;padding:.25rem 0;border-bottom:3px solid transparent}
nav.site a[aria-current=page]{border-bottom-color:var(--ink)}
nav.site a:focus-visible,.backlink:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
#manageview{padding:0 clamp(1rem,4vw,3rem) 3rem;max-width:72rem}
#manageview .mhead,#manageview .mnote,#manageview .mtools,#manageview #mlist{padding-left:0;padding-right:0}
#manageview #mlist{overflow:visible}
a.followbtn{text-decoration:none;text-align:center;display:inline-block}
.seebtn{font:inherit;font-size:.82rem;background:none;border:0;padding:0;color:var(--teal);font-weight:600;cursor:pointer;text-align:left}
dialog{border:1px solid var(--ink);background:var(--paper);color:var(--ink);padding:0;width:min(62rem,calc(100vw - 2rem));max-height:88vh;font-family:var(--font)}
dialog[open]{display:flex;flex-direction:column}
dialog::backdrop{background:rgba(21,38,44,.55)}
.mhead{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:1rem 1.2rem .4rem}
#mtitle{margin:0;font-size:1.4rem;font-weight:800;font-stretch:115%}
.mnote{margin:0;padding:0 1.2rem .7rem;color:var(--dim);font-size:.9rem}
.mtools{display:flex;flex-wrap:wrap;gap:.6rem 1.2rem;align-items:center;padding:.4rem 1.2rem .9rem;border-bottom:1px solid var(--ink)}
.mtools label{display:flex;gap:.5rem;align-items:center}
#mlist{list-style:none;margin:0;padding:0 1.2rem 1rem;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(19rem,1fr));column-gap:2.2rem;align-content:start}
#mlist li{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.6rem 0;border-bottom:1px solid var(--rule)}
#mlist .mletter{grid-column:1/-1;display:block;font-weight:800;font-stretch:115%;font-size:1.15rem;padding:.9rem 0 .25rem;border-bottom:2px solid var(--ink)}
#mlist .mrequest{grid-column:1/-1;justify-content:flex-start;gap:.8rem;flex-wrap:wrap;color:var(--dim)}
#mlist .mmore{grid-column:1/-1;justify-content:center;border-bottom:0}
.mcount{margin:.1rem 0 0;color:var(--dim);font-size:.9rem}
.mtools label{color:var(--dim)}
.mtools input[type=search]{flex:1 1 14rem;max-width:22rem}
.mname{font-weight:700;display:block}
.mmeta{display:block;color:var(--dim);font-size:.82rem}
.followbtn{font:inherit;font-weight:700;padding:.3rem .8rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink);cursor:pointer;min-width:6.5rem}
.followbtn[aria-pressed=true]{background:var(--ink);color:var(--paper)}
.followbtn:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
.notice{margin:0;padding:.6rem clamp(1rem,4vw,3rem);background:var(--sticker);color:var(--ink);font-weight:600}
.mtools input[type=email]{font:inherit;padding:.3rem .6rem;border:1px solid var(--ink);background:var(--paper);color:var(--ink);flex:1 1 14rem;max-width:22rem}
#sform .mnote,#sout .mnote{padding-top:.5rem}
footer{padding:0 clamp(1rem,4vw,3rem) 3rem;color:var(--dim);font-size:.85rem;max-width:70ch}
</style>
</head>
<body>
<header>
  <div class="headrow"><h1>New from the labels you follow</h1><button type="button" class="manage" id="authBtn">Sign in</button></div>
  <p class="meta" id="meta"></p>
</header>
<nav class="site" aria-label="Pages"><a href="#/" id="navHome">New releases</a><a href="#/labels" id="navLabels">Labels</a><a href="#/artists" id="navArtists">Artists</a><a href="#/wishlist" id="navWishlist">Wishlist</a><a href="#/basket" id="navBasket">Basket</a></nav>
<p class="notice" id="notice" role="status" hidden></p>
<div id="homeview">
<div class="bar">
  <div class="tabs" id="tabs"></div>
  <div class="tabs" id="srctabs" role="group" aria-label="Releases from"></div>
  <span id="count" aria-live="polite"></span>
  <div class="controls">
    <input type="search" id="q" placeholder="Search artist, title or catalogue number" aria-label="Search artist, title or catalogue number">
    <details class="multi" id="labels"><summary>All my labels</summary><div class="panel" id="labelpanel"></div></details>
    <details class="multi" id="genres"><summary>All genres</summary><div class="panel" id="genrepanel"></div></details>
    <label>Shop <select id="shop"></select></label>
    <label>Sort <select id="sort"></select></label>
    <label class="check"><input type="checkbox" id="hide"> Hide sold out</label>
  </div>
</div>
<main class="grid" id="grid"></main>
<p class="empty" id="empty" hidden>No releases match. Choose All to see everything.</p>
<footer>Totals are the price of one record plus shipping to Sydney, converted to Australian dollars. Shipping per record drops when you buy several from the same shop.</footer>
</div>
<section id="entityview" hidden aria-labelledby="etitle">
  <div class="mhead"><div><h2 id="etitle"></h2><p class="mcount" id="esub"></p></div><div class="eacts"><button type="button" class="followbtn" id="efollow"></button><a class="followbtn backlink" href="#/">Back to releases</a></div></div>
  <p class="mnote" id="elinks"></p>
  <main class="grid" id="egrid"></main>
</section>
<section id="wishview" hidden aria-labelledby="wtitle">
  <div class="mhead"><div><h2 id="wtitle">Wishlist</h2><p class="mcount" id="wsub"></p></div><a class="followbtn backlink" href="#/">Back to releases</a></div>
  <p class="mnote" id="wnote">Records you might buy. They stay here, out of your basket, until you move them across or remove them.</p>
  <main class="grid" id="wgrid"></main>
</section>
<section id="basketview" hidden aria-labelledby="btitle">
  <div class="mhead"><div><h2 id="btitle">Basket</h2><p class="mcount" id="bsub"></p></div><a class="followbtn backlink" href="#/">Back to releases</a></div>
  <div id="bbody"></div>
</section>
<section id="manageview" hidden aria-labelledby="mtitle">
  <div class="mhead"><div><h2 id="mtitle"></h2><p class="mcount" id="mcount"></p></div><a class="followbtn backlink" href="#/">Back to releases</a></div>
  <p class="mnote" id="mnote"></p>
  <div class="mtools">
    <input type="search" id="mq" placeholder="Search" aria-label="Search">
    <label>Sort <select id="msort"><option value="n">Most releases</option><option value="az">A to Z</option><option value="followed">Followed first</option></select></label>
    <label>Show <select id="mshow"><option value="all">All</option><option value="following">Following</option><option value="not">Not following</option><option value="full">Full catalogue</option></select></label>
    <label>Genre <select id="mgenre"></select></label>
    <label>Releases <select id="mmin"><option value="1">Any</option><option value="2">2 or more</option><option value="5">5 or more</option><option value="10">10 or more</option></select></label>
  </div>
  <ul id="mlist"></ul>
</section>
<dialog id="signin" aria-labelledby="stitle">
  <div class="mhead"><h2 id="stitle">Sign in</h2><button type="button" class="followbtn" id="sclose">Close</button></div>
  <form id="sform">
    <p class="mnote">Enter your email and we'll send you a sign-in link. There's no password. Your follows are then saved to your account and follow you to other devices.</p>
    <div class="mtools"><input type="email" id="semail" placeholder="you@example.com" aria-label="Email address" required autocomplete="email"><button type="submit" class="followbtn" id="ssend">Email me a link</button></div>
    <p class="mnote" id="sstatus" aria-live="polite"></p>
  </form>
  <div id="sout" hidden>
    <p class="mnote">Signed in as <strong id="swho"></strong>. Your follows are saved to your account.</p>
    <div class="mtools"><button type="button" class="followbtn" id="ssignout">Sign out</button></div>
  </div>
</dialog>
<div class="player" id="player" role="region" aria-label="Preview player" hidden>
  <img id="pimg" alt="" hidden>
  <div><div class="ptrack" id="ptrack"></div><div class="psub" id="psub"></div></div>
  <div class="pctl"><button type="button" id="pprev" aria-label="Previous track"></button><button type="button" id="pplay" aria-label="Play or pause"></button><button type="button" id="pnext" aria-label="Next track"></button></div>
  <div class="pseek"><span id="pcur">0:00</span><input type="range" id="pseek" min="0" max="1000" value="0" aria-label="Seek"><span id="pdur">0:00</span></div>
  <button type="button" id="pclose" aria-label="Close player">Close</button>
  <div class="pextra">
    <label>Volume <input type="range" id="pvol" min="0" max="100" value="100" aria-label="Volume"></label>
    <label>Tempo <input type="range" id="ptempo" min="-16" max="16" step="0.5" value="0" aria-label="Tempo adjust, percent"> <output id="ptempoval">0%</output></label>
    <button type="button" id="ptempo0">Reset tempo</button>
    <label class="pkey"><input type="checkbox" id="pkey" checked> Key lock</label>
  </div>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const STATUS = {in_stock:["In stock","ok"], preorder:["Pre-order","pre"], sold_out:["Sold out","out"], unknown:["Stock unknown","unk"]};
const VIEWS = [["all","All"],["in_stock","In stock"],["preorder","Pre-order"],["sold_out","Sold out"]];
const state = {view:"all", labels:new Set(), shop:"all", q:"", hide:false, sort:"new", genres:new Set(), src:"both"};
const SKEY = "crate-compare-src", BKEY = "crate-compare-basket", WKEY = "crate-compare-wishlist";
try { const v = localStorage.getItem(SKEY); if (v === "labels" || v === "artists") state.src = v; } catch (e) {}
let basket = new Set();
try { basket = new Set(JSON.parse(localStorage.getItem(BKEY) || "[]")); } catch (e) {}
let wish = new Set();
try { wish = new Set(JSON.parse(localStorage.getItem(WKEY) || "[]")); } catch (e) {}
function saveSaved(){ try { localStorage.setItem(BKEY, JSON.stringify([...basket])); localStorage.setItem(WKEY, JSON.stringify([...wish])); } catch (e) {} }
// a record can be in the basket, on the wishlist, both or neither
function setSaved(r, list, on){
  const set = list === "basket" ? basket : wish;
  on ? set.add(r.key) : set.delete(r.key);
  saveSaved(); updateManageButton(); pushSaved(r.key, r.title, list, on);
}
const norm = s => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[\s\-_.]+/g, "");
D.releases.forEach((r, i) => {
  r.id = i;
  r.key = r.lkey + "|" + (r.cat || "") + "|" + r.title.toLowerCase();
  r.hay = norm(r.title + " " + r.artists + " " + r.cat);
  r.addedMs = r.added ? Date.parse(r.added.replace(" ", "T") + "Z") : 0;
});
// "New" badges: releases first seen after your previous visit. A visit ends after an hour away,
// so refreshing the page doesn't clear the badges.
let since = null;
try {
  const now = Date.now();
  let v = JSON.parse(localStorage.getItem("crate-compare-visit") || "{}");
  if (!v.last || now - v.last > 3600000) v = {since: v.last || null, last: now}; else v.last = now;
  localStorage.setItem("crate-compare-visit", JSON.stringify(v));
  since = v.since;
} catch (e) {}
const isNew = r => since != null && r.addedMs > since;
// The labels you follow decide what's on the home page. Saved in this browser; the first visit starts with the defaults.
const FKEY = "crate-compare-follows";
let followed = new Set(D.defaults);
try { const saved = localStorage.getItem(FKEY); if (saved) followed = new Set(JSON.parse(saved)); } catch (e) {}
const money = n => "A$" + n.toFixed(2);
const nice = s => new Date(s + "T00:00:00").toLocaleDateString("en-AU", {day:"numeric", month:"short", year:"numeric"});
const safe = u => /^https?:\/\//.test(u || "") ? u : null;
const avail = o => o.status === "in_stock" || o.status === "preorder";
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

const metaEl = document.getElementById("meta");
metaEl.textContent =
  "Prices are delivered to Sydney in Australian dollars. Rates on " + nice(D.generated) +
  ": 1 GBP = A$" + D.gbp.toFixed(2) + ", 1 EUR = A$" + D.eur.toFixed(2) + ".";

const srctabs = document.getElementById("srctabs");
[["both", "Labels and artists"], ["labels", "Labels"], ["artists", "Artists"]].forEach(([key, name]) => {
  const b = el("button", "", name); b.type = "button";
  b.setAttribute("aria-pressed", key === state.src);
  b.onclick = () => {
    state.src = key; [...srctabs.children].forEach(x => x.setAttribute("aria-pressed", x === b));
    try { localStorage.setItem(SKEY, key); } catch (e) {}
    buildGenreFilter(); render();
  };
  srctabs.append(b);
});
const tabs = document.getElementById("tabs");
VIEWS.forEach(([key, name]) => {
  const b = el("button", "", name); b.type = "button";
  b.setAttribute("aria-pressed", key === state.view);
  b.onclick = () => { state.view = key; [...tabs.children].forEach(x => x.setAttribute("aria-pressed", x === b)); render(); };
  tabs.append(b);
});

function fill(id, key, allText, values){
  const sel = document.getElementById(id);
  ["all", ...values].forEach(v => { const o = el("option", "", v === "all" ? allText : v); o.value = v; sel.append(o); });
  sel.onchange = () => { state[key] = sel.value; render(); };
}
const uniq = f => [...new Set(D.releases.map(f))].filter(Boolean).sort();
const labelBox = document.getElementById("labels");
const labelSummary = labelBox.querySelector("summary");
const labelPanel = document.getElementById("labelpanel");
const NAME = {}; D.labels.forEach(l => { NAME[l.key] = l.name; }); D.artists.forEach(a => { NAME[a.key] = a.name; });
const LG = {}, AG = {};   // how many releases each label / artist has in each genre
D.releases.forEach(r => r.genres.forEach(g => {
  (LG[r.lkey] = LG[r.lkey] || {})[g] = (LG[r.lkey][g] || 0) + 1;
  r.akeys.forEach(k => { (AG[k] = AG[k] || {})[g] = (AG[k][g] || 0) + 1; });
}));
const topGenres = o => Object.entries(o || {}).sort((a, b) => b[1] - a[1]).slice(0, 3).map(e => e[0]);
const isFollowedRel = r => followed.has(r.lkey) || r.akeys.some(k => followed.has(k));
const inSource = r => state.src === "labels" ? followed.has(r.lkey)
                    : state.src === "artists" ? r.akeys.some(k => followed.has(k)) : isFollowedRel(r);
const clearBtn = el("button", "clearbtn", "Clear selection"); clearBtn.type = "button";
function labelText(){
  const n = state.labels.size;
  labelSummary.textContent = n === 0 ? "All my labels" : n === 1 ? NAME[[...state.labels][0]] : n + " labels";
}
function buildLabelFilter(){
  const counts = {};
  D.releases.forEach(r => { if (followed.has(r.lkey)) counts[r.lkey] = (counts[r.lkey] || 0) + 1; });
  [...state.labels].forEach(k => { if (!counts[k]) state.labels.delete(k); });
  labelPanel.replaceChildren();
  Object.keys(counts).sort((a, b) => NAME[a].localeCompare(NAME[b])).forEach(key => {
    const row = el("label"), cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = key; cb.checked = state.labels.has(key);
    cb.onchange = () => { cb.checked ? state.labels.add(key) : state.labels.delete(key); labelText(); render(); };
    row.append(cb, el("span", "", NAME[key] + " (" + counts[key] + ")")); labelPanel.append(row);
  });
  labelPanel.append(clearBtn);
  labelText();
}
clearBtn.onclick = () => { state.labels.clear(); labelPanel.querySelectorAll("input").forEach(c => c.checked = false); labelText(); render(); };
const genreBox = document.getElementById("genres");
const genreSummary = genreBox.querySelector("summary");
const genrePanel = document.getElementById("genrepanel");
const NOGENRE = "Non-electronic";
const gclear = el("button", "clearbtn", "Clear selection"); gclear.type = "button";
const relGenres = r => r.genres.length ? r.genres : [NOGENRE];
function genreText(){
  const n = state.genres.size;
  genreSummary.textContent = n === 0 ? "All genres" : n === 1 ? [...state.genres][0] : n + " genres";
}
function buildGenreFilter(){
  const counts = {};
  D.releases.forEach(r => { if (inSource(r)) relGenres(r).forEach(g => { counts[g] = (counts[g] || 0) + 1; }); });
  [...state.genres].forEach(g => { if (!counts[g]) state.genres.delete(g); });
  const order = D.genreOrder || [];
  const rank = g => g === NOGENRE ? 1e6 : (order.indexOf(g) < 0 ? 1e5 : order.indexOf(g));
  genrePanel.replaceChildren();
  Object.keys(counts).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)).forEach(g => {
    const row = el("label"), cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = g; cb.checked = state.genres.has(g);
    cb.onchange = () => { cb.checked ? state.genres.add(g) : state.genres.delete(g); genreText(); render(); };
    row.append(cb, el("span", "", g + " (" + counts[g] + ")")); genrePanel.append(row);
  });
  genrePanel.append(gclear);
  genreText();
}
gclear.onclick = () => { state.genres.clear(); genrePanel.querySelectorAll("input").forEach(c => c.checked = false); genreText(); render(); };
document.addEventListener("click", e => {
  if (!labelBox.contains(e.target)) labelBox.open = false;
  if (!genreBox.contains(e.target)) genreBox.open = false;
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && labelBox.open) { labelBox.open = false; labelSummary.focus(); }
  if (e.key === "Escape" && genreBox.open) { genreBox.open = false; genreSummary.focus(); }
});
fill("shop", "shop", "All shops", [...new Set(D.releases.flatMap(r => r.offers.map(o => o.shop)))].sort());
document.getElementById("q").oninput = e => { state.q = norm(e.target.value); render(); };
document.getElementById("hide").onchange = e => { state.hide = e.target.checked; render(); };

// Cheapest delivered price: copies you can buy count first
D.releases.forEach(r => {
  const ok = r.offers.filter(o => avail(o) && o.total != null).map(o => o.total);
  const any = r.offers.filter(o => o.total != null).map(o => o.total);
  r.low = ok.length ? Math.min(...ok) : (any.length ? Math.min(...any) : null);
});
const byNumber = (f, dir) => (a, b) => {
  const x = f(a), y = f(b);
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  return dir * (x - y);
};
const upcoming = r => r.date > D.generated;
const SORTS = [
  ["new", "Newest first", (a, b) => b.date.localeCompare(a.date)],
  ["old", "Oldest first", (a, b) => (a.date || "9").localeCompare(b.date || "9")],
  ["due", "Due soonest", (a, b) => upcoming(a) !== upcoming(b) ? (upcoming(a) ? -1 : 1)
      : upcoming(a) ? a.date.localeCompare(b.date) : b.date.localeCompare(a.date)],
  ["low", "Price, low to high", byNumber(r => r.low, 1)],
  ["high", "Price, high to low", byNumber(r => r.low, -1)],
  ["added", "Recently added", (a, b) => b.added.slice(0, 13).localeCompare(a.added.slice(0, 13)) || b.date.localeCompare(a.date)],
];
const sortSel = document.getElementById("sort");
SORTS.forEach(([key, name]) => { const o = el("option", "", name); o.value = key; sortSel.append(o); });
sortSel.onchange = () => { state.sort = sortSel.value; render(); };

function matches(r){
  if (!inSource(r)) return false;
  if (state.labels.size && !state.labels.has(r.lkey)) return false;
  if (state.genres.size && !relGenres(r).some(g => state.genres.has(g))) return false;
  if (state.q && !r.hay.includes(state.q)) return false;
  const offs = state.shop === "all" ? r.offers : r.offers.filter(o => o.shop === state.shop);
  if (!offs.length) return false;
  if (state.hide && !offs.some(avail)) return false;
  if (state.view === "all") return true;
  if (state.view === "sold_out") return !offs.some(avail);
  return offs.some(o => o.status === state.view);
}

function savedButtons(r){
  const box = el("div", "savedacts"), bb = el("button", "basketbtn"), wb = el("button", "basketbtn alt");
  bb.type = wb.type = "button";
  const buyable = r.offers.some(o => avail(o) && o.price != null);
  const paint = () => {
    const inB = basket.has(r.key), inW = wish.has(r.key);
    bb.textContent = inB ? "In basket (remove)" : "Add to basket"; bb.setAttribute("aria-pressed", inB);
    wb.textContent = inW ? "Saved for later (remove)" : "Save for later"; wb.setAttribute("aria-pressed", inW);
    bb.hidden = !buyable && !inB;
  };
  bb.onclick = () => { setSaved(r, "basket", !basket.has(r.key)); paint(); if (location.hash === "#/wishlist") renderWishlist(); };
  wb.onclick = () => { setSaved(r, "wishlist", !wish.has(r.key)); paint(); if (location.hash === "#/wishlist") renderWishlist(); };
  paint(); box.append(bb, wb);
  return box;
}
function artCandidates(u){            // try the biggest version of the cover first, then fall back to the one we scraped
  if (!safe(u)) return [];
  const out = [];
  if (/imagescdn\.juno\.co\.uk\//.test(u)) out.push(u.replace(/imagescdn\.juno\.co\.uk\/\d+\//, "imagescdn.juno.co.uk/full/").replace(/-MED(\.\w+)$/, "-BIG$1"));
  if (/yoyaku\.io\/wp-content\//.test(u)) out.push(u.replace(/-\d+x\d+(\.\w+)$/, "$1"));
  out.push(u);
  return [...new Set(out)];
}
function loadArt(img, urls, fail){
  let i = 0;
  img.onerror = () => { i++; if (i < urls.length) img.src = urls[i]; else fail(); };
  img.src = urls[0];
}
function entityLink(text, kind, key){ return Object.assign(el("a", "entlink", text), {href: "#/" + kind + "/" + encodeURIComponent(key)}); }
function card(r){
  const a = el("article", "sleeve");
  a.dataset.rel = r.id;
  const src = source(r);
  const art = el("div", "art");
  const none = () => el("div", "noart", r.cat || r.title);
  if (safe(r.art)) {
    const img = document.createElement("img");
    img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    loadArt(img, artCandidates(r.art), () => img.replaceWith(none()));
    art.append(img);
  } else art.append(none());
  if (isNew(r)) art.append(el("div", "newtag", "New"));
  if (src) {
    const pb = el("button", "playbtn"); pb.type = "button"; pb.innerHTML = ICON.play; pb.setAttribute("aria-label", "Play preview");
    pb.onclick = () => { if (P.rel === r) { audio.paused ? audio.play().catch(() => {}) : audio.pause(); } else startTracks(r, src, 0); };
    art.append(pb);
  }
  if (r.best) {
    const s = el("div", "sticker"); s.append(el("span", "", "cheapest at"), el("b", "", r.best)); art.append(s);
  }
  a.append(art, el("h2", "title", r.title));
  if (r.anames.length) {
    const by = el("p", "by");
    r.anames.forEach(([name, key], i) => { if (i) by.append(", "); by.append(key ? entityLink(name, "artist", key) : document.createTextNode(name)); });
    a.append(by);
  }
  const info = el("p", "info");
  if (r.label) info.append(entityLink(r.label, "label", r.lkey));
  const bits = [r.cat, r.format];
  if (r.date) bits.push((r.date > D.generated ? "due " : "released ") + nice(r.date));
  const rest = bits.filter(Boolean).join(", ");
  if (rest) info.append((r.label ? ", " : "") + rest);
  a.append(info);
  const ul = el("ul", "offers");
  r.offers.forEach(o => {
    const li = el("li", "offer" + (avail(o) ? "" : " gone"));
    const left = el("div"), st = STATUS[o.status] || STATUS.unknown;
    const shop = safe(o.url) ? Object.assign(el("a", "shop", o.shop), {href:o.url, target:"_blank", rel:"noopener"}) : el("span", "shop", o.shop);
    left.append(shop, el("span", "status " + st[1], st[0]));
    if (o.tracks && o.tracks.length) {
      const pb = el("button", "listen", "Play preview"); pb.type = "button"; pb.onclick = () => startTracks(r, o, 0);
      left.append(pb);
    } else if (safe(o.preview)) left.append(Object.assign(el("a", "listen", "Listen"), {href:o.preview, target:"_blank", rel:"noopener"}));
    const right = el("div", "cost");
    if (o.total != null) right.append(el("strong", "", money(o.total)), el("small", "", money(o.price) + " + " + money(o.ship) + " shipping"));
    else right.append(el("small", "", "Price unknown"));
    li.append(left, right); ul.append(li);
  });
  if (src) a.append(trackList(r, src));
  a.append(ul);
  a.append(savedButtons(r));
  return a;
}

// ---- Preview player ----
const ICON = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
  prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6h2v12H6zM9.5 12 18 6v12z"/></svg>',
  next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 6h2v12h-2zM6 6l8.5 6L6 18z"/></svg>'
};
const $ = id => document.getElementById(id);
const audio = new Audio(); audio.preload = "none";
const P = {queue: [], i: -1, rel: null, offer: null, errors: 0};
let currentList = [];
const bar = $("player"), pplay = $("pplay"), pseek = $("pseek");
$("pprev").innerHTML = ICON.prev; $("pnext").innerHTML = ICON.next; pplay.innerHTML = ICON.play;
const clock = s => isFinite(s) ? Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0") : "0:00";
const source = r => r.offers.find(o => o.tracks && o.tracks.length) || null;

function markPlaying(){
  document.querySelectorAll(".sleeve").forEach(c => {
    const mine = !!P.rel && +c.dataset.rel === P.rel.id;
    c.classList.toggle("playing", mine);
    const b = c.querySelector(".playbtn");
    if (b) { b.innerHTML = mine && !audio.paused ? ICON.pause : ICON.play; b.setAttribute("aria-label", mine && !audio.paused ? "Pause preview" : "Play preview"); }
  });
  document.querySelectorAll(".tracks button").forEach(b =>
    b.classList.toggle("on", !!P.rel && +b.dataset.rel === P.rel.id && b.dataset.shop === P.offer.shop && +b.dataset.i === P.i));
}
function startTracks(r, offer, index){
  P.queue = offer.tracks; P.i = index; P.rel = r; P.offer = offer; P.errors = 0;
  loadTrack();
}
function loadTrack(){
  const t = P.queue[P.i];
  audio.src = t.url; applyTempo();
  audio.play().catch(() => {});
  bar.hidden = false; document.body.classList.add("has-player");
  $("ptrack").textContent = t.title;
  $("psub").textContent = [P.rel.artists, P.rel.title].filter(Boolean).join(", ") + ". Preview from " + P.offer.shop;
  const img = $("pimg"); img.hidden = !safe(P.rel.art); if (safe(P.rel.art)) loadArt(img, artCandidates(P.rel.art), () => { img.hidden = true; });
  markPlaying();
  try {
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: P.rel.artists, album: P.rel.title, artwork: safe(P.rel.art) ? [{src: P.rel.art}] : []});
      navigator.mediaSession.setActionHandler("previoustrack", prevTrack);
      navigator.mediaSession.setActionHandler("nexttrack", nextTrack);
    }
  } catch (e) {}
}
function nextTrack(){
  if (P.i + 1 < P.queue.length) { P.i++; loadTrack(); return; }
  const at = currentList.indexOf(P.rel);      // end of this release: carry on to the next one in your list
  if (at >= 0) for (let k = at + 1; k < currentList.length; k++) {
    const o = source(currentList[k]);
    if (o) { startTracks(currentList[k], o, 0); return; }
  }
  audio.pause();
}
function prevTrack(){
  if (audio.currentTime > 3 || P.i === 0) { audio.currentTime = 0; return; }
  P.i--; loadTrack();
}
const PKEY = "crate-compare-player";
const tune = {vol: 100, tempo: 0, keylock: true};
try { Object.assign(tune, JSON.parse(localStorage.getItem(PKEY) || "{}")); } catch (e) {}
function applyTempo(){
  const rate = 1 + tune.tempo / 100;
  audio.defaultPlaybackRate = rate; audio.playbackRate = rate;       // set both: a new track resets the rate to the default
  audio.preservesPitch = audio.mozPreservesPitch = audio.webkitPreservesPitch = tune.keylock;
}
function paintTune(){
  $("pvol").value = tune.vol; $("ptempo").value = tune.tempo; $("pkey").checked = tune.keylock;
  $("ptempoval").textContent = (tune.tempo > 0 ? "+" : "") + tune.tempo.toFixed(1).replace(/\.0$/, "") + "%";
  audio.volume = tune.vol / 100; applyTempo();
  try { localStorage.setItem(PKEY, JSON.stringify(tune)); } catch (e) {}
}
$("pvol").oninput = e => { tune.vol = +e.target.value; paintTune(); };
$("ptempo").oninput = e => { tune.tempo = +e.target.value; paintTune(); };
$("ptempo0").onclick = () => { tune.tempo = 0; paintTune(); };
$("pkey").onchange = e => { tune.keylock = e.target.checked; paintTune(); };
paintTune();
audio.addEventListener("loadedmetadata", applyTempo);
audio.addEventListener("play", () => { pplay.innerHTML = ICON.pause; markPlaying(); });
audio.addEventListener("pause", () => { pplay.innerHTML = ICON.play; markPlaying(); });
audio.addEventListener("playing", () => { P.errors = 0; });
audio.addEventListener("ended", nextTrack);
audio.addEventListener("loadedmetadata", () => { $("pdur").textContent = clock(audio.duration); });
audio.addEventListener("timeupdate", () => {
  $("pcur").textContent = clock(audio.currentTime);
  pseek.value = audio.duration ? Math.round(audio.currentTime / audio.duration * 1000) : 0;
});
audio.addEventListener("error", () => {
  if (!P.rel) return;
  P.errors++;
  if (P.errors <= 2 && P.i + 1 < P.queue.length) { nextTrack(); return; }
  $("ptrack").textContent = "This preview couldn't be loaded";
  $("psub").textContent = "Try it on " + P.offer.shop + " instead.";
});
pplay.onclick = () => { if (!audio.src) return; audio.paused ? audio.play().catch(() => {}) : audio.pause(); };
$("pprev").onclick = prevTrack; $("pnext").onclick = nextTrack;
pseek.oninput = () => { if (audio.duration) audio.currentTime = pseek.value / 1000 * audio.duration; };
$("pclose").onclick = () => {
  audio.pause(); audio.removeAttribute("src"); P.rel = null; bar.hidden = true;
  document.body.classList.remove("has-player"); markPlaying();
};
document.addEventListener("keydown", e => {
  if (e.code === "Space" && !bar.hidden && !/^(INPUT|SELECT|TEXTAREA|BUTTON|SUMMARY)$/.test(e.target.tagName)) { e.preventDefault(); pplay.click(); }
});
function trackList(r, o){
  const d = el("details", "tracks"), ol = el("ol");
  o.tracks.forEach((t, i) => {
    const li = el("li"), b = el("button", "", t.title);
    b.type = "button"; b.dataset.rel = r.id; b.dataset.shop = o.shop; b.dataset.i = i;
    b.onclick = () => startTracks(r, o, i);
    li.append(b); ol.append(li);
  });
  d.append(el("summary", "", "Tracks (" + o.tracks.length + ")"), ol, el("p", "src", "Previews from " + o.shop));
  return d;
}

function render(){
  const grid = document.getElementById("grid"); grid.replaceChildren();
  const list = D.releases.filter(matches).sort(SORTS.find(x => x[0] === state.sort)[2]);
  currentList = list;
  list.forEach(r => grid.append(card(r)));
  markPlaying();
  document.getElementById("count").textContent = list.length + (list.length === 1 ? " release" : " releases");
  const emptyEl = document.getElementById("empty");
  emptyEl.hidden = list.length > 0;
  emptyEl.textContent = (state.src === "artists" && nFollowed("artists") === 0) ? "You aren't following any artists yet. Open Artists to pick some."
                      : (state.src === "labels" && nFollowed("labels") === 0) ? "You aren't following any labels yet. Open Labels to pick some."
                      : followed.size === 0 ? "You aren't following any labels or artists yet. Open Labels or Artists to pick some."
                                            : "No releases match. Choose All to see everything.";
}
// ---- Manage labels (follow / unfollow) ----
const mlist = $("mlist");
let mKind = "labels";
function saveFollows(){ try { localStorage.setItem(FKEY, JSON.stringify([...followed])); } catch (e) {} }
function updateManageButton(){ $("navLabels").textContent = "Labels (" + nFollowed("labels") + ")"; $("navArtists").textContent = "Artists (" + nFollowed("artists") + ")"; $("navBasket").textContent = "Basket (" + basket.size + ")"; $("navWishlist").textContent = "Wishlist (" + wish.size + ")"; }
const nFollowed = kind => [...followed].filter(k => k.startsWith("a:") === (kind === "artists")).length;
function followChanged(){ saveFollows(); updateManageButton(); buildLabelFilter(); buildGenreFilter(); render(); }
let mShown = 200;
function renderManage(reset = true){
  if (reset) mShown = 200;
  const q = norm($("mq").value), show = $("mshow").value, minN = +$("mmin").value, sortBy = $("msort").value;
  const kind = mKind, items = kind === "artists" ? D.artists : D.labels, GM = kind === "artists" ? AG : LG, gsel = $("mgenre").value;
  $("mshow").querySelector('[value="full"]').hidden = kind === "artists";
  if (kind === "artists" && $("mshow").value === "full") $("mshow").value = "all";
  const rows = items.filter(l => (!gsel || (GM[l.key] || {})[gsel]) &&
    (!q || norm(l.name).includes(q)) && l.n >= minN &&
    (show === "all" || (show === "following" && followed.has(l.key)) || (show === "not" && !followed.has(l.key)) || (show === "full" && l.full)));
  const byName = (a, b) => a.name.localeCompare(b.name, "en", {sensitivity: "base"});
  if (sortBy === "az") rows.sort(byName);
  else if (sortBy === "followed") rows.sort((a, b) => (followed.has(b.key) - followed.has(a.key)) || (b.n - a.n) || byName(a, b));
  else rows.sort((a, b) => (b.n - a.n) || byName(a, b));
  $("mcount").textContent = "Showing " + rows.length + " of " + items.length + " " + kind + ". You follow " + nFollowed(kind) + ".";
  mlist.replaceChildren();
  let lastLetter = "";
  rows.slice(0, mShown).forEach(l => {
    if (sortBy === "az") {
      const first = (l.name.normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim()[0] || "#").toUpperCase();
      const letter = /[A-Z]/.test(first) ? first : "#";
      if (letter !== lastLetter) { lastLetter = letter; mlist.append(el("li", "mletter", letter)); }
    }
    const li = el("li"), info = el("div"), btn = el("button", "followbtn");
    const tg = topGenres(GM[l.key]);
    info.append(Object.assign(el("a", "mname entlink", l.name), {href: "#/" + (kind === "artists" ? "artist" : "label") + "/" + encodeURIComponent(l.key)}),
                el("span", "mmeta", l.n + (l.n === 1 ? " release" : " releases") + (l.full ? ", full catalogue" : "") + (tg.length ? " · " + tg.join(", ") : "")));
    if (followed.has(l.key)) {
      const see = el("button", "seebtn", "See releases"); see.type = "button";
      see.onclick = () => showReleases(kind, l);
      info.append(see);
    }
    btn.type = "button";
    const paint = () => {
      const on = followed.has(l.key);
      btn.textContent = on ? "Following" : "Follow";
      btn.setAttribute("aria-pressed", on);
      btn.setAttribute("aria-label", (on ? "Unfollow " : "Follow ") + l.name);
    };
    btn.onclick = () => {
      followed.has(l.key) ? followed.delete(l.key) : followed.add(l.key);
      paint(); followChanged(); pushFollow(l.key, followed.has(l.key));
      if (!(show === "following" || show === "not" || sortBy === "followed")) renderManage(false);
      if (show === "following" || show === "not" || sortBy === "followed") renderManage(false);
      else $("mcount").textContent = $("mcount").textContent.replace(/You follow \d+\./, "You follow " + nFollowed(kind) + ".");
    };
    paint(); li.append(info, btn); mlist.append(li);
  });
  if (!rows.length) mlist.append(el("li", "", "Nothing matches."));
  if (q.length >= 2) mlist.append(requestRow(kind, $("mq").value.trim()));
  if (rows.length > mShown) {
    const more = el("li", "mmore"), mb = el("button", "followbtn", "Show more (" + (rows.length - mShown) + " left)");
    mb.type = "button"; mb.onclick = () => { mShown += 200; renderManage(false); };
    more.append(mb); mlist.append(more);
  }
}
// ---- Ask us to add an artist or label that isn't listed ----
const REQ_KEY = "crate-compare-requests";
function requestedBefore(kind, name){ try { return (JSON.parse(localStorage.getItem(REQ_KEY)) || []).includes(kind + ":" + name.toLowerCase()); } catch (e) { return false; } }
function rememberRequest(kind, name){ try { const a = JSON.parse(localStorage.getItem(REQ_KEY)) || []; a.push(kind + ":" + name.toLowerCase()); localStorage.setItem(REQ_KEY, JSON.stringify(a)); } catch (e) {} }
function requestRow(kind, name){
  const one = kind === "artists" ? "artist" : "label";
  const li = el("li", "mrequest"), msg = el("span", "", "Can't find " + one + " \u201c" + name + "\u201d? "), btn = el("button", "followbtn", "Request it");
  btn.type = "button";
  const done = () => { btn.hidden = true; msg.textContent = "Requested \u2014 we'll look for \u201c" + name + "\u201d at the shops. It should appear after the next daily refresh."; };
  if (requestedBefore(one, name)) done();
  btn.onclick = async () => {
    if (!SB || !session) { notice("Sign in first (top right) so we can add your request."); return; }
    btn.disabled = true;
    try {
      await sb("/rest/v1/requests", {method: "POST", headers: {Prefer: "resolution=merge-duplicates,return=minimal"},
               body: JSON.stringify([{user_id: jwtClaims(session.access_token).sub, kind: one, name: name.slice(0, 120)}])});
      rememberRequest(one, name); done();
    } catch (err) { btn.disabled = false; notice("Couldn't send that request: " + err.message); }
  };
  li.append(msg, btn); return li;
}
// ---- The Labels and Artists pages (the address ends #/labels or #/artists) ----
function showReleases(kind, item){
  state.q = ""; $("q").value = ""; state.labels.clear();
  if (kind === "labels") state.labels.add(item.key);
  else { state.q = norm(item.name); $("q").value = item.name; }
  buildLabelFilter(); render(); location.hash = "#/";
}
function fillGenreSelect(){
  const sel = $("mgenre"), present = new Set();
  D.releases.forEach(r => r.genres.forEach(g => present.add(g)));
  const order = (D.genreOrder || []).filter(g => present.has(g));
  sel.replaceChildren();
  [["", "Any genre"], ...order.map(g => [g, g])].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; sel.append(o); });
}
function route(){
  const ent = location.hash.match(/^#\/(label|artist)\/(.+)$/);
  const h = location.hash, kind = ent ? "entity" : h === "#/labels" ? "labels" : h === "#/artists" ? "artists" : h === "#/basket" ? "basket" : h === "#/wishlist" ? "wishlist" : "home";
  $("homeview").hidden = kind !== "home"; $("manageview").hidden = kind !== "labels" && kind !== "artists"; $("basketview").hidden = kind !== "basket"; $("wishview").hidden = kind !== "wishlist"; $("entityview").hidden = kind !== "entity";
  [["navHome", "home"], ["navLabels", "labels"], ["navArtists", "artists"], ["navBasket", "basket"], ["navWishlist", "wishlist"]].forEach(([id, k]) => {
    if (k === kind) $(id).setAttribute("aria-current", "page"); else $(id).removeAttribute("aria-current");
  });
  if (kind === "basket") { renderBasket(); window.scrollTo(0, 0); }
  if (kind === "wishlist") { renderWishlist(); window.scrollTo(0, 0); }
  if (kind === "entity") { renderEntity(ent[1], decodeURIComponent(ent[2])); window.scrollTo(0, 0); }
  if (kind === "labels" || kind === "artists") {
    if (mKind !== kind) { mKind = kind; $("mq").value = ""; $("mshow").value = "all"; $("mgenre").value = ""; }
    $("mtitle").textContent = kind === "labels" ? "Labels" : "Artists";
    $("mnote").textContent = kind === "labels"
      ? 'Follow a label to put its releases on your home page. Labels marked "full catalogue" are tracked in full; for the others you see what the shops\' new-release pages catch.'
      : "Follow an artist to see their releases on your home page, even from labels you don't follow. Their full catalogue is fetched from each shop every morning.";
    renderManage();
    window.scrollTo(0, 0);
  }
}
// ---- Basket: the cheapest way to split your records across shops, shipping included ----
const shopCost = (name, k) => k ? D.shops[name].first + D.shops[name].extra * (k - 1) : 0;
function bestOffers(r){
  const m = {};
  r.offers.forEach(o => { if (avail(o) && o.price != null && (!m[o.shop] || o.price < m[o.shop].price)) m[o.shop] = o; });
  return m;
}
function planBasket(items){
  const names = Object.keys(D.shops), opts = items.map(bestOffers);
  const idx = items.map((_, i) => i).filter(i => Object.keys(opts[i]).length);
  const masks = [];
  for (let m = 1; m < (1 << names.length); m++) masks.push(m);
  const pop = m => m.toString(2).replace(/0/g, "").length;
  masks.sort((a, b) => pop(a) - pop(b) || a - b);                 // fewer shops first, so ties go to fewer parcels
  const costOf = assign => {
    const per = {};
    assign.forEach(([i, s]) => { per[s] = per[s] || {n: 0, items: 0}; per[s].n += 1; per[s].items += opts[i][s].price; });
    return Object.entries(per).reduce((t, [s, v]) => t + v.items + shopCost(s, v.n), 0);
  };
  let best = null;
  for (const m of masks) {
    const S = names.filter((_, j) => m & (1 << j)), assign = [];
    let ok = true;
    for (const i of idx) {
      let pick = null, pc = Infinity;
      S.forEach(s => { const o = opts[i][s]; if (o && o.price + D.shops[s].extra < pc) { pc = o.price + D.shops[s].extra; pick = s; } });
      if (!pick) { ok = false; break; }
      assign.push([i, pick]);
    }
    if (!ok) continue;
    const total = costOf(assign);
    if (!best || total < best.total - 0.005) best = {assign, total};
  }
  // for comparison: every record bought on its own from its cheapest shop, and everything from one shop
  const separate = idx.reduce((t, i) => t + Math.min(...Object.entries(opts[i]).map(([s, o]) => o.price + D.shops[s].first)), 0);
  const single = names.filter(s => idx.every(i => opts[i][s])).map(s => ({
    shop: s, total: idx.reduce((t, i) => t + opts[i][s].price, 0) + shopCost(s, idx.length)}))
    .sort((a, b) => a.total - b.total)[0] || null;
  return {best, opts, separate, single, missing: items.filter((_, i) => !idx.includes(i))};
}
function renderEntity(type, key){
  const isLabel = type === "label", list = isLabel ? D.labels : D.artists, e = list.find(x => x.key === key);
  const grid = $("egrid"); grid.replaceChildren(); $("elinks").replaceChildren();
  if (!e) { $("etitle").textContent = "Not found"; $("esub").textContent = ""; $("efollow").hidden = true; return; }
  const rels = D.releases.filter(r => isLabel ? r.lkey === key : r.akeys.includes(key)).sort((a, b) => b.date.localeCompare(a.date));
  const tg = topGenres((isLabel ? LG : AG)[key]);
  $("etitle").textContent = e.name;
  $("esub").textContent = (isLabel ? "Label" : "Artist") + " · " + rels.length + (rels.length === 1 ? " release" : " releases") + (tg.length ? " · " + tg.join(", ") : "");
  const fb = $("efollow"); fb.hidden = false;
  const paint = () => { const on = followed.has(key); fb.textContent = on ? "Following" : "Follow"; fb.setAttribute("aria-pressed", on); fb.setAttribute("aria-label", (on ? "Unfollow " : "Follow ") + e.name); };
  fb.onclick = () => { followed.has(key) ? followed.delete(key) : followed.add(key); paint(); followChanged(); pushFollow(key, followed.has(key)); };
  paint();
  const shops = Object.entries(e.pages || {}).filter(([, u]) => safe(u));
  if (shops.length) {
    $("elinks").append("Their page at: ");
    shops.forEach(([shop, u], i) => { if (i) $("elinks").append(", "); $("elinks").append(Object.assign(el("a", "entlink", shop), {href: u, target: "_blank", rel: "noopener"})); });
  }
  rels.slice(0, 300).forEach(r => grid.append(card(r)));
  if (!rels.length) grid.append(el("p", "mnote", "No releases found yet."));
}
function renderWishlist(){
  const grid = $("wgrid"); grid.replaceChildren();
  const items = D.releases.filter(r => wish.has(r.key)).sort((a, b) => b.date.localeCompare(a.date));
  $("wsub").textContent = items.length + (items.length === 1 ? " record" : " records") + " saved for later";
  items.forEach(r => grid.append(card(r)));
  if (!items.length) grid.append(el("p", "mnote", "Nothing saved yet. Use Save for later on any release."));
}
function renderBasket(){
  const body = $("bbody"); body.replaceChildren();
  const items = D.releases.filter(r => basket.has(r.key));
  $("bsub").textContent = items.length + (items.length === 1 ? " record" : " records") + " in your basket";
  if (!items.length) {
    body.append(el("p", "mnote", "Your basket is empty. Use Add to basket on any release (or move one over from your wishlist) and this page works out the cheapest way to buy them, shipping included."));
    return;
  }
  const plan = planBasket(items), byShop = {};
  if (plan.best) plan.best.assign.forEach(([i, s]) => { (byShop[s] = byShop[s] || []).push(i); });
  const sum = el("div", "bsum");
  if (plan.best) {
    const shopsUsed = Object.keys(byShop).length;
    sum.append(el("strong", "", money(plan.best.total)),
               el("span", "", " delivered to Sydney, from " + shopsUsed + (shopsUsed === 1 ? " shop" : " shops")));
    const notes = [];
    if (plan.separate - plan.best.total > 0.5) notes.push("Saves " + money(plan.separate - plan.best.total) + " compared with buying each record on its own at its cheapest shop (" + money(plan.separate) + ").");
    if (plan.single && plan.single.total - plan.best.total > 0.5) notes.push("Buying everything at " + plan.single.shop + " would cost " + money(plan.single.total) + ".");
    notes.forEach(t => sum.append(el("p", "mmeta", t)));
  } else sum.append(el("span", "", "None of these records can be bought right now."));
  body.append(sum);
  Object.keys(byShop).sort().forEach(s => {
    const idxs = byShop[s], box = el("section", "bshop");
    const sub = idxs.reduce((t, i) => t + plan.opts[i][s].price, 0), ship = shopCost(s, idxs.length);
    const h = el("h3"); h.append(el("span", "", s), el("span", "", idxs.length + (idxs.length === 1 ? " record" : " records") + " · " + money(sub + ship)));
    const ul = el("ul");
    idxs.forEach(i => {
      const r = items[i], o = plan.opts[i][s], li = el("li"), left = el("div"), right = el("div");
      left.append(el("span", "bt", r.title), el("span", "bm", [r.artists, r.label, r.cat].filter(Boolean).join(", ") + (o.status === "preorder" ? " · pre-order" : "")));
      const rm = el("button", "rmbtn", "Remove"); rm.type = "button";
      rm.onclick = () => { setSaved(r, "basket", false); renderBasket(); render(); };
      const later = el("button", "rmbtn", wish.has(r.key) ? "On wishlist" : "Save for later"); later.disabled = wish.has(r.key); later.type = "button";
      later.onclick = () => { setSaved(r, "wishlist", true); later.textContent = "On wishlist"; later.disabled = true; render(); };
      right.append(safe(o.url) ? Object.assign(el("a", "shop", money(o.price)), {href: o.url, target: "_blank", rel: "noopener"}) : el("span", "", money(o.price)), document.createTextNode(" "), later, document.createTextNode(" "), rm);
      li.append(left, right); ul.append(li);
    });
    const foot = el("div", "bfoot"); foot.append(el("span", "", "Shipping to Sydney"), el("span", "", money(ship)));
    box.append(h, ul, foot); body.append(box);
  });
  if (plan.missing.length) {
    const box = el("section", "bshop"), h = el("h3"); h.append(el("span", "", "Not available right now"), el("span", "", String(plan.missing.length)));
    const ul = el("ul");
    plan.missing.forEach(r => {
      const li = el("li"), left = el("div"), rm = el("button", "rmbtn", "Remove"); rm.type = "button";
      rm.onclick = () => { setSaved(r, "basket", false); renderBasket(); render(); };
      left.append(el("span", "bt", r.title), el("span", "bm", [r.artists, r.label, r.cat].filter(Boolean).join(", ")));
      li.append(left, rm); ul.append(li);
    });
    box.append(h, ul); body.append(box);
  }
  const act = el("div", "bactions"), copy = el("button", "followbtn", "Copy shopping list"), clear = el("button", "followbtn", "Clear basket");
  copy.type = clear.type = "button";
  copy.onclick = async () => {
    const lines = Object.keys(byShop).sort().flatMap(s => [s + ":", ...byShop[s].map(i => "- " + items[i].artists + " - " + items[i].title + " (" + items[i].cat + ") " + plan.opts[i][s].url), ""]);
    try { await navigator.clipboard.writeText(lines.join("\n")); copy.textContent = "Copied"; } catch (e) { copy.textContent = "Couldn't copy"; }
  };
  clear.onclick = () => { [...basket].forEach(k => { basket.delete(k); pushSaved(k, "", "basket", false); }); saveSaved(); updateManageButton(); renderBasket(); render(); };
  act.append(copy, clear); body.append(act);
  body.append(el("p", "mmeta", "Shipping is estimated from each shop's rates to Sydney. Pre-orders often ship on release day, so they can arrive separately. Check each shop's cart before paying. Your basket and wishlist are saved to your account when you are signed in, and on this device."));
}
window.addEventListener("hashchange", route);
$("mq").oninput = () => renderManage(); ["msort", "mshow", "mmin", "mgenre"].forEach(id => { $(id).onchange = () => renderManage(); });
updateManageButton(); buildLabelFilter(); buildGenreFilter(); fillGenreSelect(); route();

// ---- Accounts: sign in by emailed link; follows saved to your account (Supabase) ----
const SB = D.supabase;
const AKEY = "crate-compare-auth";
let session = null;
const sdlg = $("signin");
const notice = text => { const n = $("notice"); n.textContent = text || ""; n.hidden = !text; };
function jwtClaims(t){
  try {
    const p = t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(atob(p).split("").map(c => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join("")));
  } catch (e) { return {}; }
}
function makeSession(access, refresh){
  const c = jwtClaims(access);
  return {access_token: access, refresh_token: refresh, expires_at: c.exp || Date.now() / 1000 + 3000, email: c.email || ""};
}
function storeSession(s){
  session = s;
  try { s ? localStorage.setItem(AKEY, JSON.stringify(s)) : localStorage.removeItem(AKEY); } catch (e) {}
}
async function sb(path, opts = {}, authed = true){
  const headers = Object.assign({apikey: SB.key, "Content-Type": "application/json"}, opts.headers || {});
  if (authed) {
    await refreshIfNeeded();
    if (!session) throw new Error("not signed in");
    headers.Authorization = "Bearer " + session.access_token;
  }
  const res = await fetch(SB.url + path, Object.assign({}, opts, {headers}));
  const text = await res.text();
  let body = null; try { body = text ? JSON.parse(text) : null; } catch (e) {}
  if (!res.ok) {
    const msg = body && (body.msg || body.message || body.error_description || body.error) || res.statusText || "request failed";
    throw new Error(msg + " (" + res.status + ")");
  }
  return body;
}
async function refreshIfNeeded(){
  if (!session || session.expires_at - Date.now() / 1000 > 60) return;
  try {
    const t = await sb("/auth/v1/token?grant_type=refresh_token", {method: "POST", body: JSON.stringify({refresh_token: session.refresh_token})}, false);
    storeSession(makeSession(t.access_token, t.refresh_token));
  } catch (e) { storeSession(null); updateAuthUi(); notice("You've been signed out. Sign in again to sync your follows."); }
}
function updateAuthUi(){
  $("authBtn").textContent = session ? "Sign out" : "Sign in"; $("authBtn").title = session ? "Signed in as " + (session.email || "your account") : "";
  $("sform").hidden = !!session; $("sout").hidden = !session;
  $("stitle").textContent = session ? "Your account" : "Sign in";
  $("swho").textContent = session ? (session.email || "your account") : "";
}
function pushFollow(key, on){
  if (!SB || !session) return;
  const call = on
    ? sb("/rest/v1/follows", {method: "POST", headers: {Prefer: "resolution=ignore-duplicates,return=minimal"},
                              body: JSON.stringify([{label_key: key, label_name: NAME[key] || key}])})
    : sb("/rest/v1/follows?label_key=eq." + encodeURIComponent(key), {method: "DELETE"});
  call.catch(err => { if (session) notice("Couldn't save that to your account: " + err.message); });
}
function pushSaved(key, title, list, on){
  if (!SB || !session) return;
  const call = on
    ? sb("/rest/v1/saved_items", {method: "POST", headers: {Prefer: "resolution=merge-duplicates,return=minimal"},
                                  body: JSON.stringify([{user_id: jwtClaims(session.access_token).sub, item_key: key, list, title}])})
    : sb("/rest/v1/saved_items?item_key=eq." + encodeURIComponent(key) + "&list=eq." + list, {method: "DELETE"});
  call.catch(err => { if (session) notice("Couldn't save that to your account: " + err.message); });
}
async function syncSaved(){
  if (!SB || !session) return;
  try {
    const rows = await sb("/rest/v1/saved_items?select=item_key,list,title") || [];
    if (rows.length) {                                                       // your account is the source of truth
      basket = new Set(rows.filter(r => r.list === "basket").map(r => r.item_key));
      wish = new Set(rows.filter(r => r.list === "wishlist").map(r => r.item_key));
      saveSaved();
    } else if (basket.size || wish.size) {                                   // first sign-in: save what's on this device
      const uid = jwtClaims(session.access_token).sub, title = k => (D.releases.find(r => r.key === k) || {}).title || "";
      await sb("/rest/v1/saved_items", {method: "POST", headers: {Prefer: "resolution=merge-duplicates,return=minimal"},
               body: JSON.stringify([...[...basket].map(k => ({user_id: uid, item_key: k, list: "basket", title: title(k)})),
                                     ...[...wish].map(k => ({user_id: uid, item_key: k, list: "wishlist", title: title(k)}))])});
    }
    updateManageButton(); render();
    if (location.hash === "#/basket") renderBasket();
    if (location.hash === "#/wishlist") renderWishlist();
  } catch (err) { if (session) notice("Couldn't sync your basket and wishlist: " + err.message); }
}
async function syncFollows(){
  if (!SB || !session) return;
  try {
    const rows = await sb("/rest/v1/follows?select=label_key") || [];
    if (rows.length) followed = new Set(rows.map(r => r.label_key));      // your account is the source of truth
    else if (followed.size) {                                              // first sign-in: save what you already follow
      await sb("/rest/v1/follows", {method: "POST", headers: {Prefer: "resolution=ignore-duplicates,return=minimal"},
               body: JSON.stringify([...followed].map(k => ({label_key: k, label_name: NAME[k] || k})))});
    }
    followChanged(); if (!$("manageview").hidden) renderManage(false);
  } catch (err) { if (session) notice("Couldn't sync your follows with your account: " + err.message); }
}
(function startAccounts(){
  if (!SB) { $("authBtn").hidden = true; return; }
  const h = new URLSearchParams(location.hash.replace(/^#/, ""));
  if (h.get("access_token") && h.get("refresh_token")) {                    // arriving from the emailed link
    storeSession(makeSession(h.get("access_token"), h.get("refresh_token")));
    history.replaceState(null, "", location.pathname + location.search);
    notice("You're signed in.");
  } else if (h.get("error_description")) {
    notice("Sign-in didn't work: " + h.get("error_description").replace(/\+/g, " ") + ". Try sending a new link.");
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    try { session = JSON.parse(localStorage.getItem(AKEY) || "null"); } catch (e) {}
  }
  updateAuthUi();
  $("authBtn").onclick = () => { if (session) { $("ssignout").onclick(); return; } updateAuthUi(); sdlg.showModal ? sdlg.showModal() : sdlg.setAttribute("open", ""); };
  $("sclose").onclick = () => { sdlg.close ? sdlg.close() : sdlg.removeAttribute("open"); };
  sdlg.addEventListener("click", e => { if (e.target === sdlg) $("sclose").click(); });
  $("sform").onsubmit = async e => {
    e.preventDefault();
    const email = $("semail").value.trim(), st = $("sstatus");
    if (!/^https?:$/.test(location.protocol)) { st.textContent = "Sign-in only works on the hosted page, not on a file opened from your computer."; return; }
    st.textContent = "Sending..."; $("ssend").disabled = true;
    try {
      await sb("/auth/v1/otp?redirect_to=" + encodeURIComponent(location.origin + location.pathname),
               {method: "POST", body: JSON.stringify({email, create_user: true})}, false);
      st.textContent = "Check your email for a sign-in link. Open it on this device and you'll come back here signed in.";
    } catch (err) { st.textContent = "Couldn't send the link: " + err.message; }
    $("ssend").disabled = false;
  };
  $("ssignout").onclick = () => {
    storeSession(null); updateAuthUi(); $("sclose").click();
    notice("Signed out. Your follows stay on this browser.");
  };
  syncFollows(); syncSaved();
})();

const nNew = D.releases.filter(isNew).length;
if (nNew) metaEl.textContent += " " + nNew + (nNew === 1 ? " release is" : " releases are") + " new since your last visit.";
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
