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


def build():
    rates = scraper.get_rates()
    con = scraper.connect()
    rows = con.execute(
        """SELECT r.id, r.title, COALESCE(lb.name, '') AS label, r.catalogue_no, r.format,
                  r.release_date, r.artwork_url,
                  (SELECT group_concat(a.name, ', ') FROM release_artists ra
                     JOIN artists a ON a.id = ra.artist_id WHERE ra.release_id = r.id) AS artists,
                  rt.name AS shop, rt.currency AS ship_cur, rt.ship_first_item AS ship,
                  li.price, li.currency, li.status, li.url, li.preview_url, li.first_seen
           FROM releases r
           LEFT JOIN labels lb ON lb.id = r.label_id
           JOIN listings li ON li.release_id = r.id
           JOIN retailers rt ON rt.id = li.retailer_id
           WHERE r.label_id IN (SELECT entity_id FROM follows WHERE entity_type = 'label')
           ORDER BY r.id"""
    ).fetchall()

    releases = {}
    for r in rows:
        rel = releases.setdefault(r["id"], {
            "title": r["title"], "artists": r["artists"] or "", "label": r["label"],
            "cat": r["catalogue_no"] or "", "format": r["format"] or "",
            "date": r["release_date"] or "", "art": r["artwork_url"] or "", "added": "", "offers": [],
        })
        rel["added"] = min(rel["added"] or r["first_seen"], r["first_seen"])
        price = ship = total = None
        if r["price"] is not None and r["ship"] is not None:
            price = round(r["price"] * rates[r["currency"]], 2)
            ship = round(r["ship"] * rates[r["ship_cur"]], 2)
            total = round(price + ship, 2)
        rel["offers"].append({
            "shop": r["shop"], "status": r["status"], "price": price, "ship": ship,
            "total": total, "url": r["url"], "preview": r["preview_url"] or "",
        })

    out = []
    for rel in releases.values():
        buyable = [o for o in rel["offers"] if o["status"] in AVAILABLE and o["total"] is not None]
        rel["best"] = min(buyable, key=lambda o: o["total"])["shop"] if len(buyable) > 1 else ""
        rel["offers"].sort(key=lambda o: (o["status"] not in AVAILABLE, o["total"] or 1e9))
        out.append(rel)
    out.sort(key=lambda x: x["date"], reverse=True)

    data = {
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
header{padding:2.2rem clamp(1rem,4vw,3rem) 1rem}
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
footer{padding:0 clamp(1rem,4vw,3rem) 3rem;color:var(--dim);font-size:.85rem;max-width:70ch}
</style>
</head>
<body>
<header>
  <h1>New from the labels you follow</h1>
  <p class="meta" id="meta"></p>
</header>
<div class="bar">
  <div class="tabs" id="tabs"></div>
  <span id="count" aria-live="polite"></span>
  <div class="controls">
    <input type="search" id="q" placeholder="Search artist, title or catalogue number" aria-label="Search artist, title or catalogue number">
    <details class="multi" id="labels"><summary>All labels</summary><div class="panel" id="labelpanel"></div></details>
    <label>Shop <select id="shop"></select></label>
    <label>Format <select id="format"></select></label>
    <label>Sort <select id="sort"></select></label>
    <label class="check"><input type="checkbox" id="hide"> Hide sold out</label>
  </div>
</div>
<main class="grid" id="grid"></main>
<p class="empty" id="empty" hidden>No releases match. Choose All to see everything.</p>
<footer>Totals are the price of one record plus shipping to Sydney, converted to Australian dollars. Shipping per record drops when you buy several from the same shop.</footer>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const STATUS = {in_stock:["In stock","ok"], preorder:["Pre-order","pre"], sold_out:["Sold out","out"], unknown:["Stock unknown","unk"]};
const VIEWS = [["all","All"],["in_stock","In stock"],["preorder","Pre-order"],["sold_out","Sold out"]];
const state = {view:"all", labels:new Set(), shop:"all", format:"all", q:"", hide:false, sort:"new"};
const norm = s => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[\s\-_.]+/g, "");
D.releases.forEach(r => {
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
const money = n => "A$" + n.toFixed(2);
const nice = s => new Date(s + "T00:00:00").toLocaleDateString("en-AU", {day:"numeric", month:"short", year:"numeric"});
const safe = u => /^https?:\/\//.test(u || "") ? u : null;
const avail = o => o.status === "in_stock" || o.status === "preorder";
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

const metaEl = document.getElementById("meta");
metaEl.textContent =
  "Prices are delivered to Sydney in Australian dollars. Rates on " + nice(D.generated) +
  ": 1 GBP = A$" + D.gbp.toFixed(2) + ", 1 EUR = A$" + D.eur.toFixed(2) + ".";

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
const counts = {};
D.releases.forEach(r => { if (r.label) counts[r.label] = (counts[r.label] || 0) + 1; });
function labelText(){
  const n = state.labels.size;
  labelSummary.textContent = n === 0 ? "All labels" : n === 1 ? [...state.labels][0] : n + " labels";
}
Object.keys(counts).sort().forEach(name => {
  const row = el("label"), cb = document.createElement("input");
  cb.type = "checkbox"; cb.value = name;
  cb.onchange = () => { cb.checked ? state.labels.add(name) : state.labels.delete(name); labelText(); render(); };
  row.append(cb, el("span", "", name + " (" + counts[name] + ")")); labelPanel.append(row);
});
const clearBtn = el("button", "clearbtn", "Clear selection"); clearBtn.type = "button";
clearBtn.onclick = () => { state.labels.clear(); labelPanel.querySelectorAll("input").forEach(c => c.checked = false); labelText(); render(); };
labelPanel.append(clearBtn);
document.addEventListener("click", e => { if (!labelBox.contains(e.target)) labelBox.open = false; });
document.addEventListener("keydown", e => { if (e.key === "Escape" && labelBox.open) { labelBox.open = false; labelSummary.focus(); } });
fill("shop", "shop", "All shops", [...new Set(D.releases.flatMap(r => r.offers.map(o => o.shop)))].sort());
fill("format", "format", "All formats", uniq(r => r.format));
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
  if (state.labels.size && !state.labels.has(r.label)) return false;
  if (state.format !== "all" && r.format !== state.format) return false;
  if (state.q && !r.hay.includes(state.q)) return false;
  const offs = state.shop === "all" ? r.offers : r.offers.filter(o => o.shop === state.shop);
  if (!offs.length) return false;
  if (state.hide && !offs.some(avail)) return false;
  if (state.view === "all") return true;
  if (state.view === "sold_out") return !offs.some(avail);
  return offs.some(o => o.status === state.view);
}

function card(r){
  const a = el("article", "sleeve");
  const art = el("div", "art");
  const none = () => el("div", "noart", r.cat || r.title);
  if (safe(r.art)) {
    const img = document.createElement("img");
    img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = r.art;
    img.onerror = () => { img.replaceWith(none()); };
    art.append(img);
  } else art.append(none());
  if (isNew(r)) art.append(el("div", "newtag", "New"));
  if (r.best) {
    const s = el("div", "sticker"); s.append(el("span", "", "cheapest at"), el("b", "", r.best)); art.append(s);
  }
  a.append(art, el("h2", "title", r.title));
  if (r.artists) a.append(el("p", "by", r.artists));
  const bits = [r.label, r.cat, r.format];
  if (r.date) bits.push((r.date > D.generated ? "due " : "released ") + nice(r.date));
  a.append(el("p", "info", bits.filter(Boolean).join(", ")));
  const ul = el("ul", "offers");
  r.offers.forEach(o => {
    const li = el("li", "offer" + (avail(o) ? "" : " gone"));
    const left = el("div"), st = STATUS[o.status] || STATUS.unknown;
    const shop = safe(o.url) ? Object.assign(el("a", "shop", o.shop), {href:o.url, target:"_blank", rel:"noopener"}) : el("span", "shop", o.shop);
    left.append(shop, el("span", "status " + st[1], st[0]));
    if (safe(o.preview)) left.append(Object.assign(el("a", "listen", "Listen"), {href:o.preview, target:"_blank", rel:"noopener"}));
    const right = el("div", "cost");
    if (o.total != null) right.append(el("strong", "", money(o.total)), el("small", "", money(o.price) + " + " + money(o.ship) + " shipping"));
    else right.append(el("small", "", "Price unknown"));
    li.append(left, right); ul.append(li);
  });
  a.append(ul);
  return a;
}

function render(){
  const grid = document.getElementById("grid"); grid.replaceChildren();
  const list = D.releases.filter(matches).sort(SORTS.find(x => x[0] === state.sort)[2]);
  list.forEach(r => grid.append(card(r)));
  document.getElementById("count").textContent = list.length + (list.length === 1 ? " release" : " releases");
  document.getElementById("empty").hidden = list.length > 0;
}
const nNew = D.releases.filter(isNew).length;
if (nNew) metaEl.textContent += " " + nNew + (nNew === 1 ? " release is" : " releases are") + " new since your last visit.";
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
