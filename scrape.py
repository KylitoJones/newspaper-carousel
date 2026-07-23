#!/usr/bin/env python3
"""
scrape.py — Build today's front-page manifest from three sources:
Freedom Forum, FrontPages.com, and Kiosko.net.

Papers are de-duplicated across sources and ordered by REGION PRIORITY
(see REGION_ORDER below) so the front of the grid is US, then Canada, Mexico,
Western Europe, Central America, South America, then everywhere else.

    python scrape.py                 # scrape now, write site/manifest.json  (force scrape)
    python scrape.py --date 2026-07-21
    python scrape.py --verbose

Output: site/manifest.json  (the browse page reads this file)
"""

import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
SITE_DIR = HERE / "site"
MANIFEST_PATH = SITE_DIR / "manifest.json"
ALIASES_PATH = HERE / "aliases.json"
DENVER = ZoneInfo("America/Denver")
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")}
MAX_WORKERS = 16
REQUEST_TIMEOUT = 20

# ---------------------------------------------------------------------------
# REGION PRIORITY  — edit freely. Papers are grouped and shown in THIS order.
# Country codes follow Kiosko's scheme (note: "uk", not "gb").
# Anything not listed here falls into the final "Everywhere else" bucket.
# ---------------------------------------------------------------------------
REGION_ORDER = [
    ("United States",              {"us"}),
    ("Canada",                     {"ca"}),
    ("Mexico",                     {"mx"}),
    ("Western Europe",             {"uk","ie","fr","de","nl","be","ch","at",
                                    "es","it","pt","ad","no","se","dk","fi","is","lu"}),
    ("Central America & Caribbean",{"gt","hn","sv","ni","cr","pa","do","cu","pr"}),
    ("South America",              {"ar","bo","br","cl","co","ec","py","pe","uy","ve"}),
]
OTHER_REGION = "Everywhere else"

def region_of(country: str):
    """Return (rank, region_name) for a country code."""
    for i, (name, codes) in enumerate(REGION_ORDER):
        if country in codes:
            return i, name
    return len(REGION_ORDER), OTHER_REGION

# Map FrontPages.com country labels -> Kiosko-style codes (best effort).
COUNTRY_NAME_TO_CODE = {
    "us":"us","usa":"us","uk":"uk","england":"uk","scotland":"uk","wales":"uk",
    "northern ireland":"uk","ireland":"ie","france":"fr","germany":"de",
    "netherlands":"nl","belgium":"be","switzerland":"ch","austria":"at","spain":"es",
    "italy":"it","portugal":"pt","andorra":"ad","norway":"no","sweden":"se",
    "denmark":"dk","finland":"fi","canada":"ca","mexico":"mx","argentina":"ar",
    "brazil":"br","brasil":"br","chile":"cl","colombia":"co","peru":"pe","uruguay":"uy",
    "venezuela":"ve","ecuador":"ec","bolivia":"bo","paraguay":"py","guatemala":"gt",
    "honduras":"hn","costa rica":"cr","panama":"pa","nicaragua":"ni","el salvador":"sv",
    "cuba":"cu","dominican rep":"do","puerto rico":"pr","china":"cn","india":"in",
    "japan":"jp","australia":"au","new zealand":"nz","israel":"il","iran":"ir",
    "singapore":"sg","south korea":"kr","taiwan":"tw","thailand":"th","pakistan":"pk",
    "indonesia":"id","vietnam":"vn","malaysia":"my","philippines":"ph","bangladesh":"bd",
    "uae":"ae","united arab emirates":"ae","qatar":"qa","saudi arabia":"sa","jordan":"jo",
    "kenya":"ke","nigeria":"ng","south africa":"za","egypt":"eg","morocco":"ma",
    "malta":"mt","albania":"al","turkey":"tr","russia":"ru","poland":"pl","greece":"gr",
    "croatia":"hr",
}

def log(m): print(m, flush=True)

def normalize_name(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\s*\|\s*.*$", "", n)
    n = re.sub(r"\b(the|el|la|le|il)\b", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()

def load_aliases() -> dict:
    if ALIASES_PATH.exists():
        try:
            raw = json.loads(ALIASES_PATH.read_text())
            return {normalize_name(k): normalize_name(v) for k, v in raw.items()}
        except Exception as e:
            log(f"  ! could not read aliases.json: {e}")
    return {}

def dedup_key(name, aliases):
    k = normalize_name(name)
    return aliases.get(k, k)

# ---------------------------------------------------------------------------
# Freedom Forum  (almost entirely US; default country = us)
# ---------------------------------------------------------------------------
FF_CDN = "d2dr22b2lm4tvw.cloudfront.net"
FF_IMG_RE = re.compile(r"https://%s/([a-z0-9_\-]+)/(\d{4}-\d{2}-\d{2})/front-page-large\.jpg"
                       % re.escape(FF_CDN), re.I)
FF_LINK_RE = re.compile(r"/newspapers/([a-z0-9]+(?:_[a-z0-9]+)*)-([^\"'?<]+)", re.I)

def scrape_freedomforum(target, verbose=False):
    papers, names = {}, {}
    for url in ("https://frontpages.freedomforum.org/",
                "https://frontpages.freedomforum.org/gallery"):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
        except Exception as e:
            log(f"  ! Freedom Forum fetch failed ({url}): {e}"); continue
        html = r.text
        for code, raw in FF_LINK_RE.findall(html):
            code = code.lower()
            if code not in names:
                names[code] = raw.replace("_", " ").strip()
        for code, d in FF_IMG_RE.findall(html):
            code = code.lower()
            papers[code] = {"date": d, "image": f"https://{FF_CDN}/{code}/{d}/front-page-large.jpg"}
    for code, name in names.items():
        papers.setdefault(code, {"date": target,
            "image": f"https://{FF_CDN}/{code}/{target}/front-page-large.jpg"})
    out = []
    for code, info in papers.items():
        name = names.get(code)
        if not name: continue
        out.append({"name": name, "date": info["date"], "image": info["image"],
                    "thumb": info["image"], "source": "Freedom Forum", "country": "us",
                    "page_url": f"https://frontpages.freedomforum.org/newspapers/{code}-{name.replace(' ','_')}"})
        if verbose: log(f"    FF  {name}")
    log(f"  Freedom Forum: {len(out)} papers")
    return out

# ---------------------------------------------------------------------------
# FrontPages.com  (per-paper page fetch for the cover image)
# ---------------------------------------------------------------------------
FP_BASE = "https://www.frontpages.com"
FP_CATS = {"/us-newspapers/": "us", "/uk-newspapers/": "uk",
           "/world-newspapers/": "", "/financial-newspapers/": "", "/sports-newspapers/": ""}
FP_LINK_RE = re.compile(r"^/([a-z0-9\-]+)/$", re.I)
FP_SKIP = {"us-newspapers","uk-newspapers","world-newspapers","financial-newspapers",
           "sports-newspapers","newspaper-list"}

def _fp_enumerate():
    slugs = {}  # slug -> (name, country)
    for path, cc in FP_CATS.items():
        try:
            r = requests.get(FP_BASE+path, headers=HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
        except Exception as e:
            log(f"  ! FrontPages fetch failed ({path}): {e}"); continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            m = FP_LINK_RE.match(a["href"])
            if not m: continue
            slug = m.group(1)
            if slug in FP_SKIP: continue
            strong = a.find(["strong","b"])
            name = re.sub(r"\s+"," ",(strong.get_text(strip=True) if strong else a.get_text(strip=True))).strip()
            if slug and name and slug not in slugs:
                slugs[slug] = (name, cc)
    return slugs

def _fp_fetch(slug, name, cc, verbose=False):
    url = f"{FP_BASE}/{slug}/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    def meta(p):
        t = soup.find("meta", property=p) or soup.find("meta", attrs={"name": p})
        return t["content"].strip() if t and t.get("content") else None
    image = meta("og:image")
    if not image or "/g/" not in image: return None
    d = None
    upd = meta("og:updated_time")
    if upd:
        try: d = datetime.fromisoformat(upd).date().isoformat()
        except Exception: pass
    if not d:
        m = re.search(r"/g/(\d{4})/(\d{2})/(\d{2})/", image)
        if m: d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if verbose: log(f"    FP  {name}")
    return {"name": name, "date": d or "", "image": image, "thumb": image,
            "source": "FrontPages.com", "country": cc,
            "page_url": url}

def scrape_frontpages(verbose=False):
    slugs = _fp_enumerate()
    log(f"  FrontPages.com: {len(slugs)} listed, fetching covers...")
    out = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_fp_fetch, s, n, cc, verbose): s for s,(n,cc) in slugs.items()}
        for f in as_completed(futs):
            r = f.result()
            if r: out.append(r)
    log(f"  FrontPages.com: {len(out)} covers resolved")
    return out

# ---------------------------------------------------------------------------
# Kiosko.net  (index pages already contain image URLs + names; 1 fetch/region)
# ---------------------------------------------------------------------------
KIOSKO = "https://en.kiosko.net"
KIOSKO_IMG_RE = re.compile(r"https?://img\.kiosko\.net/(\d{4})/(\d{2})/(\d{2})/([a-z]{2})/([a-z0-9_\-]+)\.\d+\.jpg", re.I)
KIOSKO_COUNTRIES = ["us","ca","mx","uk","ie","fr","de","nl","be","ch","es","it","pt","ad",
    "no","se","dk","ar","bo","br","cl","co","cr","cu","do","ec","sv","gt","hn","ni","pa",
    "py","pe","pr","uy","ve","eg","ma","ng","za","au","cn","in","ir","il","jp","nz","tr",
    "ru","pl","gr","hr"]
KIOSKO_US_STATES = ["Alabama","Arizona","California","Colorado","Florida","Georgia","Illinois",
    "Indiana","Maryland","Massachusetts","Michigan","Minnesota","Missouri","New_Jersey",
    "New_York","Ohio","Oregon","Pensilvania","Texas","Washington","Washington_DC"]

def _kiosko_index_urls():
    urls = [f"{KIOSKO}/{cc}/" for cc in KIOSKO_COUNTRIES]
    urls += [f"{KIOSKO}/us/geo/{s}.html" for s in KIOSKO_US_STATES]
    urls += [f"{KIOSKO}/asi/geo/ph.html", f"{KIOSKO}/asi/geo/ae.html"]
    return urls

def _kiosko_fetch_index(url, verbose=False):
    found = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
    except Exception:
        return found
    soup = BeautifulSoup(r.text, "html.parser")
    for img in soup.find_all("img", src=True):
        m = KIOSKO_IMG_RE.match(img["src"])
        if not m: continue
        yr, mo, dy, cc, slug = m.groups()
        # name from alt: "Portada de <Name> (<COUNTRY>)"
        alt = img.get("alt", "")
        nm = re.search(r"Portada de\s+(.+?)\s*\(", alt)
        name = nm.group(1).strip() if nm else slug.replace("_", " ").title()
        key = f"{cc}/{slug}"
        found[key] = {
            "name": name, "date": f"{yr}-{mo}-{dy}", "country": cc,
            "image": f"https://img.kiosko.net/{yr}/{mo}/{dy}/{cc}/{slug}.750.jpg",
            "thumb": f"https://img.kiosko.net/{yr}/{mo}/{dy}/{cc}/{slug}.200.jpg",
            "source": "Kiosko", "page_url": f"{KIOSKO}/{cc}/np/{slug}.html",
        }
    return found

def scrape_kiosko(verbose=False):
    merged = {}
    urls = _kiosko_index_urls()
    log(f"  Kiosko.net: scanning {len(urls)} index pages...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(_kiosko_fetch_index, u, verbose) for u in urls]
        for f in as_completed(futs):
            for k, v in f.result().items():
                merged.setdefault(k, v)
    out = list(merged.values())
    if verbose:
        for p in out: log(f"    KI  [{p['country']}] {p['name']}")
    log(f"  Kiosko.net: {len(out)} papers")
    return out

# ---------------------------------------------------------------------------
# Merge + rank + write
# ---------------------------------------------------------------------------
def merge(groups, aliases, prefer="Freedom Forum"):
    by_key = {}
    for paper in [p for g in groups for p in g]:
        key = dedup_key(paper["name"], aliases)
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = paper; continue
        # inherit a country if the kept copy lacks one
        if not cur.get("country") and paper.get("country"):
            cur["country"] = paper["country"]
        if not paper.get("country") and cur.get("country"):
            paper["country"] = cur["country"]
        dn, do = paper.get("date") or "", cur.get("date") or ""
        if dn > do or (dn == do and paper["source"] == prefer):
            # keep whichever we now choose, but don't lose a known country
            paper["country"] = paper.get("country") or cur.get("country") or ""
            by_key[key] = paper
    return list(by_key.values())

def main():
    ap = argparse.ArgumentParser(description="Scrape today's newspaper front pages.")
    ap.add_argument("--date")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--prefer", default="Freedom Forum",
                    choices=["Freedom Forum","FrontPages.com","Kiosko"])
    args = ap.parse_args()

    target = args.date or datetime.now(DENVER).date().isoformat()
    log(f"Scraping front pages for {target} ...")
    t0 = time.time()
    aliases = load_aliases()

    ff = scrape_freedomforum(target, args.verbose)
    fp = scrape_frontpages(args.verbose)
    ki = scrape_kiosko(args.verbose)
    merged = merge([ff, fp, ki], aliases, prefer=args.prefer)

    if not merged:
        log("ERROR: no papers found. Not overwriting the existing manifest.")
        sys.exit(1)

    # attach region + rank, then sort by (region priority, name)
    for p in merged:
        rank, region = region_of(p.get("country") or "")
        p["region"], p["_rank"] = region, rank
    merged.sort(key=lambda p: (p["_rank"], p["name"].lower()))
    for p in merged: p.pop("_rank", None)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    # region order actually present (for the UI filter), in priority order
    seen, region_seq = set(), []
    for p in merged:
        if p["region"] not in seen:
            seen.add(p["region"]); region_seq.append(p["region"])

    manifest = {"built_at": datetime.now(DENVER).isoformat(), "target_date": target,
                "count": len(merged), "regions": region_seq, "papers": merged}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    by_src = {}
    for p in merged: by_src[p["source"]] = by_src.get(p["source"], 0) + 1
    log(f"\nWrote {MANIFEST_PATH}")
    log(f"  {len(merged)} unique papers  " + ", ".join(f"{v} {k}" for k,v in by_src.items()))
    log(f"  regions: {', '.join(region_seq)}")
    log(f"  done in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
