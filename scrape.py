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

import argparse, io, json, random, re, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image

HERE = Path(__file__).resolve().parent
SITE_DIR = HERE / "site"
MANIFEST_PATH = SITE_DIR / "manifest.json"
ALIASES_PATH = HERE / "aliases.json"

# Images are downloaded into site/img/ and served from your own domain. This
# sidesteps hotlink protection, referrer rules, and ad blockers entirely, and
# it means a paper whose image is missing simply doesn't appear (instead of
# showing a broken card). These files are published as part of the deployment
# artifact and are never committed to the repository, so nothing accumulates.
IMG_DIR = SITE_DIR / "img"
THUMB_WIDTH = 420          # what the grid displays
FULL_MAX_EDGE = 1800       # what gets posted to Bluesky
MIRROR_WORKERS = 12
DENVER = ZoneInfo("America/Denver")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

# Several of these sites only serve images when the request looks like it came
# from their own pages (hotlink protection), so we send a matching Referer.
REFERERS = {
    "Freedom Forum": "https://frontpages.freedomforum.org/",
    "FrontPages.com": "https://www.frontpages.com/",
    "Kiosko": "https://en.kiosko.net/",
}


def http_get(url, referer=None, tries=3, timeout=None, image=False):
    """GET with retries/backoff. Raises RuntimeError carrying a readable reason."""
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    if image:
        h["Accept"] = IMAGE_ACCEPT
    last = "unknown error"
    for i in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=timeout or REQUEST_TIMEOUT)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last = f"HTTP {r.status_code}"
                time.sleep(4 * (i + 1) + random.random() * 2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r
        except RuntimeError:
            raise
        except Exception as e:
            last = type(e).__name__ + ": " + str(e)[:90]
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(last)


def error_summary(errors, limit=6):
    """Group identical failure reasons so the log stays readable."""
    counts = {}
    for e in errors:
        counts[e] = counts.get(e, 0) + 1
    lines = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [f"      {n:>4} x  {msg}" for msg, n in lines]
MAX_WORKERS = 16
KIOSKO_WORKERS = 4      # kiosko drops connections when hit hard; go gently
REQUEST_TIMEOUT = 25

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
            r = http_get(url, referer="https://www.google.com/", tries=4)
        except Exception as e:
            log(f"  ! Freedom Forum unreachable ({url.rsplit('/',1)[-1] or 'home'}): {e}")
            continue
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
            r = http_get(FP_BASE + path, referer=FP_BASE + "/")
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
            # World/sports listings print the country right after the name,
            # e.g. "Marca | Spain | Thursday, July 23" -> use it for regions.
            country = cc
            if strong:
                rest = a.get_text(" ", strip=True)
                rest = rest.replace(strong.get_text(strip=True), " ", 1).strip()
                low = re.sub(r"\s+", " ", rest).lower()
                for label, code in COUNTRY_NAME_TO_CODE.items():
                    if low.startswith(label):
                        country = code
                        break
            if slug and name and slug not in slugs:
                slugs[slug] = (name, country)
    return slugs

def _fp_fetch(slug, name, cc, verbose=False):
    url = f"{FP_BASE}/{slug}/"
    try:
        r = http_get(url, referer=FP_BASE + "/")
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
# NOTE: kiosko writes these as protocol-relative ("//img.kiosko.net/...") on some
# pages, so the scheme must be optional or nothing matches at all.
KIOSKO_IMG_RE = re.compile(
    r"(?:https?:)?//img\.kiosko\.net/(\d{4})/(\d{2})/(\d{2})/([a-z]{2})/([a-z0-9_\-]+)\.\d+\.jpg", re.I)
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
    """Returns (found, error). Images may be lazy-loaded, so check several attrs."""
    found = {}
    try:
        r = http_get(url, referer=KIOSKO + "/", tries=4)
    except Exception as e:
        return found, f"{url.rsplit('/',2)[-2]}: {e}"
    soup = BeautifulSoup(r.text, "html.parser")
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or
               img.get("data-original") or img.get("data-lazy-src") or "")
        if not src and img.get("srcset"):
            src = img["srcset"].split()[0]
        m = KIOSKO_IMG_RE.match(src.strip())
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
    return found, None

def scrape_kiosko(verbose=False):
    merged = {}
    urls = _kiosko_index_urls()
    log(f"  Kiosko.net: scanning {len(urls)} index pages...")
    errors = []
    with ThreadPoolExecutor(max_workers=KIOSKO_WORKERS) as pool:
        futs = [pool.submit(_kiosko_fetch_index, u, verbose) for u in urls]
        for f in as_completed(futs):
            got, err = f.result()
            if err: errors.append(err)
            for k, v in got.items():
                merged.setdefault(k, v)
    out = list(merged.values())
    log(f"  Kiosko.net: {len(out)} papers ({len(errors)} index pages unreachable)")
    for line in error_summary([e.split(": ",1)[-1] for e in errors]):
        log(line)
    return out

# ---------------------------------------------------------------------------
# Merge + rank + write
# ---------------------------------------------------------------------------
def merge(groups, aliases, prefer="Kiosko"):
    """Combine the sources. When a paper appears more than once we pick a primary
    copy, but we KEEP the others as `alts` — if the primary's image can't be
    downloaded we can fall back to another source's copy of the same paper."""
    by_key = {}
    for paper in [p for g in groups for p in g]:
        key = dedup_key(paper["name"], aliases)
        cur = by_key.get(key)
        if cur is None:
            paper["alts"] = []
            by_key[key] = paper
            continue

        alts = cur.pop("alts", [])
        # whichever copy loses becomes a fallback for the winner
        dn, do = paper.get("date") or "", cur.get("date") or ""
        newer = dn > do or (dn == do and paper["source"] == prefer)
        winner, loser = (paper, cur) if newer else (cur, paper)
        winner["country"] = winner.get("country") or loser.get("country") or ""
        alts.append({k: loser.get(k) for k in ("image", "page_url", "source", "date")})
        winner["alts"] = alts
        by_key[key] = winner
    return list(by_key.values())

def _slug_for(paper) -> str:
    """Stable, filesystem-safe filename for a paper."""
    base = re.sub(r"[^a-z0-9]+", "-", paper["name"].lower()).strip("-")
    src = {"Freedom Forum": "ff", "FrontPages.com": "fp", "Kiosko": "ki"}.get(paper["source"], "xx")
    return f"{base[:60]}-{src}"


def _image_urls_to_try(paper):
    """Every URL worth attempting for one paper: its own address (plus filename
    variants), then the same paper from any other source."""
    out = []
    for cand in [paper] + (paper.get("alts") or []):
        u, ref = cand.get("image"), cand.get("page_url")
        if not u:
            continue
        out.append((u, ref))
        if u.endswith(".webp.jpg"):
            out.append((u[:-4], ref))            # -> .webp
        elif u.endswith(".jpg.webp"):
            out.append((u[:-5], ref))            # -> .jpg
    return out


def _mirror_one(paper):
    """Download one front page, save a full + thumb copy, return (paper, None)
    with local relative paths, or (None, reason) if it couldn't be fetched.

    The Referer matters: these sites serve images only to requests that look
    like they came from their own pages."""
    img, why, used_url = None, "no image url", None
    for url, ref in _image_urls_to_try(paper):
        try:
            r = http_get(url, referer=ref or REFERERS.get(paper["source"]),
                         tries=2, timeout=45, image=True)
            ctype = r.headers.get("Content-Type", "?").split(";")[0]
            if not ctype.startswith("image"):
                why = f"served {ctype} instead of an image"
                continue
            img = Image.open(io.BytesIO(r.content))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            used_url = url
            break
        except Exception as e:
            why = str(e)[:70]
    if img is None:
        return None, why

    slug = _slug_for(paper)
    w, h = img.size
    if w < 200 or h < 200:
        return None, f"tiny image ({w}x{h}), probably a placeholder"

    full = img
    if max(w, h) > FULL_MAX_EDGE:
        s = FULL_MAX_EDGE / max(w, h)
        full = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    thumb = img.copy()
    if w > THUMB_WIDTH:
        thumb = img.resize((THUMB_WIDTH, int(h * THUMB_WIDTH / w)), Image.LANCZOS)

    try:
        full.save(IMG_DIR / "full" / f"{slug}.jpg", "JPEG", quality=86, optimize=True)
        thumb.save(IMG_DIR / "thumb" / f"{slug}.jpg", "JPEG", quality=80, optimize=True)
    except Exception as e:
        return None, "save failed: " + str(e)[:50]

    out = dict(paper)
    out["origin"] = used_url or paper["image"]   # the URL that actually worked
    out["image"] = f"img/full/{slug}.jpg"   # relative to the site root
    out["thumb"] = f"img/thumb/{slug}.jpg"
    return out, None


def mirror(papers, verbose=False):
    """Download every front page locally. Papers that fail are dropped."""
    if IMG_DIR.exists():
        shutil.rmtree(IMG_DIR)            # start clean each morning
    (IMG_DIR / "full").mkdir(parents=True, exist_ok=True)
    (IMG_DIR / "thumb").mkdir(parents=True, exist_ok=True)

    log(f"\nMirroring {len(papers)} images locally (this is the slow part)...")
    out, errors = [], []
    with ThreadPoolExecutor(max_workers=MIRROR_WORKERS) as pool:
        futs = {pool.submit(_mirror_one, p): p for p in papers}
        for f in as_completed(futs):
            res, why = f.result()
            if res:
                out.append(res)
            else:
                errors.append(why or "unknown")
    size_mb = sum(f.stat().st_size for f in IMG_DIR.rglob("*.jpg")) / 1e6
    log(f"  mirrored {len(out)} images ({size_mb:.0f} MB), {len(errors)} could not be fetched")
    if errors:
        log("  reasons images failed:")
        for line in error_summary(errors):
            log(line)
    return out


def main():
    ap = argparse.ArgumentParser(description="Scrape today's newspaper front pages.")
    ap.add_argument("--date")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--prefer", default="Kiosko",
                    choices=["Freedom Forum","FrontPages.com","Kiosko"])
    ap.add_argument("--no-mirror", action="store_true",
                    help="skip downloading images; link to the original sites instead "
                         "(much faster, but images may not display)")
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

    if not args.no_mirror:
        mirrored = mirror(merged, args.verbose)
        if mirrored:
            merged = mirrored
        else:
            log("  ! nothing could be mirrored; falling back to original image links")

    # attach region + rank, then sort by (region priority, name)
    for p in merged:
        rank, region = region_of(p.get("country") or "")
        p["region"], p["_rank"] = region, rank
    merged.sort(key=lambda p: (p["_rank"], p["name"].lower()))
    for p in merged:
        p.pop("_rank", None); p.pop("alts", None)

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
