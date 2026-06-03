#!/usr/bin/env python3
"""
CEJ apartment watcher.

Loads the CEJ (bolig.io) listings page in a real headless browser, captures the
JSON the page fetches to render apartments, diffs it against the last-seen set,
and sends an ntfy.sh push notification for every brand-new apartment.

The site sits behind bot protection that 403s plain HTTP clients, so we drive a
full Chromium browser via Playwright instead of using requests/curl.

State (which apartments we've already notified about) lives in state/seen.json
and is committed back to the repo by the GitHub Actions workflow, so it survives
across scheduled runs.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"
DEBUG_DIR = ROOT / "debug"

# Keys that, when present on a JSON object, strongly suggest it is a property/
# apartment listing. Mix of English and Danish field names.
LISTING_HINT_KEYS = {
    "address", "adresse", "addressline", "street", "vej", "road",
    "price", "pris", "husleje", "rent", "monthlyrent", "leje",
    "rooms", "room", "vaerelser", "værelser", "antalvaerelser", "nrofrooms",
    "size", "area", "kvm", "m2", "squaremeters", "areal", "boligareal",
    "zip", "postnr", "postalcode", "city", "by",
    "propertyid", "listingid", "residenceid", "unitid",
}

# Keys whose values we try, in order, to build a human-readable title.
TITLE_KEYS = ["address", "adresse", "addressLine", "title", "name", "street", "vej"]
# Keys we try, in order, to find a stable unique id.
ID_KEYS = ["id", "uuid", "guid", "listingId", "propertyId", "residenceId",
           "unitId", "slug", "url", "href", "path"]
# Keys we try to build a clickable link.
URL_KEYS = ["url", "href", "link", "path", "slug", "permalink"]
# Keys for extra detail lines.
PRICE_KEYS = ["price", "pris", "husleje", "rent", "monthlyRent", "leje", "totalPrice"]
ROOM_KEYS = ["rooms", "room", "vaerelser", "værelser", "antalVaerelser", "nrOfRooms"]
SIZE_KEYS = ["size", "area", "kvm", "m2", "squareMeters", "areal", "boligAreal"]
CITY_KEYS = ["city", "by", "town"]


def log(msg):
    print(f"[watcher] {msg}", flush=True)


def describe_shape(node, depth=0):
    """Compact, log-friendly summary of a parsed-JSON structure."""
    if isinstance(node, dict):
        keys = list(node.keys())
        return "{" + ", ".join(keys[:12]) + ("…" if len(keys) > 12 else "") + "}"
    if isinstance(node, list):
        head = describe_shape(node[0], depth + 1) if node else ""
        return f"[{len(node)} x {head}]"
    return type(node).__name__


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def lower_keys(d):
    return {k.lower(): k for k in d.keys()} if isinstance(d, dict) else {}


def get_first(obj, candidate_keys):
    """Case-insensitively return the first present, non-empty value."""
    lk = lower_keys(obj)
    for cand in candidate_keys:
        real = lk.get(cand.lower())
        if real is not None:
            val = obj[real]
            if val not in (None, "", [], {}):
                return val
    return None


def looks_like_listing(obj):
    if not isinstance(obj, dict):
        return False
    keys = {k.lower() for k in obj.keys()}
    return len(keys & LISTING_HINT_KEYS) >= 2


def find_listing_arrays(node, found):
    """Recursively walk parsed JSON collecting arrays that mostly contain
    listing-shaped objects."""
    if isinstance(node, list):
        listingish = [x for x in node if looks_like_listing(x)]
        if listingish and len(listingish) >= max(1, len(node) // 2):
            found.append(listingish)
        for x in node:
            find_listing_arrays(x, found)
    elif isinstance(node, dict):
        for v in node.values():
            find_listing_arrays(v, found)


def normalize_listing(obj, base_url):
    title = get_first(obj, TITLE_KEYS)
    if isinstance(title, dict):
        title = get_first(title, TITLE_KEYS) or json.dumps(title, ensure_ascii=False)
    title = str(title).strip() if title is not None else "Bolig"

    city = get_first(obj, CITY_KEYS)
    price = get_first(obj, PRICE_KEYS)
    rooms = get_first(obj, ROOM_KEYS)
    size = get_first(obj, SIZE_KEYS)

    raw_url = get_first(obj, URL_KEYS)
    link = None
    if isinstance(raw_url, str):
        if raw_url.startswith("http"):
            link = raw_url
        else:
            link = "https://udlejning.cej.dk/" + raw_url.lstrip("/")

    raw_id = get_first(obj, ID_KEYS)
    if raw_id is not None:
        uid = "id:" + str(raw_id)
    else:
        # Fall back to a content hash so we can still de-dupe.
        basis = json.dumps(
            {k: obj[k] for k in sorted(obj.keys())},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        uid = "hash:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    detail_bits = []
    if city:
        detail_bits.append(str(city))
    if rooms:
        detail_bits.append(f"{rooms} vær.")
    if size:
        detail_bits.append(f"{size} m²")
    if price:
        detail_bits.append(f"{price} kr.")

    return {
        "id": uid,
        "title": title,
        "detail": " · ".join(detail_bits),
        "url": link,
    }


def extract_from_json(json_blobs, base_url):
    candidates = []
    for blob in json_blobs:
        find_listing_arrays(blob, candidates)
    if not candidates:
        return []
    # Use the largest listing array we found.
    best = max(candidates, key=len)
    listings, seen_ids = [], set()
    for obj in best:
        norm = normalize_listing(obj, base_url)
        if norm["id"] in seen_ids:
            continue
        seen_ids.add(norm["id"])
        listings.append(norm)
    return listings


def extract_from_dom(page, base_url):
    """Last-resort fallback if no listing JSON was captured: scrape anchors
    that look like apartment links."""
    listings, seen_ids = [], set()
    anchors = page.eval_on_selector_all(
        "a",
        """els => els.map(a => ({
            href: a.href,
            text: (a.innerText || '').trim().replace(/\\s+/g, ' ')
        }))""",
    )
    for a in anchors:
        href = a.get("href") or ""
        text = a.get("text") or ""
        if not href or len(text) < 4:
            continue
        # Only real navigable links — never contact/util links.
        if not href.startswith(("http://", "https://")):
            continue
        # A listing is a *detail* page, not the search/overview page itself.
        if re.search(r"(overblik|find-bolig/?$|/find-bolig\?)", href, re.I):
            continue
        if not re.search(r"(bolig|lejlighed|residence|ejendom)", href, re.I):
            continue
        uid = "url:" + href
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        listings.append({"id": uid, "title": text[:120], "detail": "", "url": href})
    return listings


def send_ntfy(server, topic, listing):
    title = listing["title"]
    body = listing["detail"] or "Ny bolig hos CEJ"
    headers = {
        "Title": f"Ny bolig: {title}".encode("utf-8"),
        "Tags": "house",
        "Priority": "high",
    }
    if listing.get("url"):
        headers["Click"] = listing["url"]
    url = f"{server.rstrip('/')}/{topic}"
    resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
    resp.raise_for_status()


def scrape(config):
    from playwright.sync_api import sync_playwright

    json_blobs = []
    json_sources = []  # parallel to json_blobs: the URL each blob came from
    DEBUG_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        def on_response(response):
            try:
                ctype = (response.headers or {}).get("content-type", "")
            except Exception:
                ctype = ""
            if "json" not in ctype.lower():
                return
            try:
                json_blobs.append(response.json())
                json_sources.append(response.url)
            except Exception:
                pass

        page.on("response", on_response)

        log(f"Navigating to {config['url']}")
        page.goto(config["url"], wait_until="networkidle", timeout=60000)
        # Give client-side rendering / lazy XHRs a moment.
        time.sleep(5)
        try:
            page.mouse.wheel(0, 4000)
            time.sleep(3)
        except Exception:
            pass

        html = page.content()
        (DEBUG_DIR / "page.html").write_text(html, encoding="utf-8")
        (DEBUG_DIR / "captured.json").write_text(
            json.dumps(json_blobs, ensure_ascii=False)[:2_000_000],
            encoding="utf-8",
        )

        # Diagnostics: what JSON did the page actually fetch?
        log(f"Captured {len(json_blobs)} JSON response(s):")
        for src, blob in zip(json_sources, json_blobs):
            log(f"  <= {src}  ::  {describe_shape(blob)}")

        listings = extract_from_json(json_blobs, config["url"])
        source = "json-api"
        if not listings:
            log("No listings found in captured JSON; falling back to DOM scrape.")
            listings = extract_from_dom(page, config["url"])
            source = "dom"
            # Diagnostics: show a sample of anchors so we can tune the selector.
            all_anchors = page.eval_on_selector_all(
                "a", "els => els.map(a => a.href).filter(Boolean)"
            )
            sample = [h for h in all_anchors if not h.startswith(("mailto:", "tel:"))]
            log(f"DOM fallback: {len(all_anchors)} anchors on page; sample hrefs:")
            for h in sample[:25]:
                log(f"    {h}")

        # Cheap bot-wall detection for clearer logs.
        if not listings and re.search(r"(captcha|cloudflare|just a moment|attention required)", html, re.I):
            log("WARNING: page looks like a bot/Cloudflare challenge — runner IP may be blocked.")

        browser.close()
    log(f"Extracted {len(listings)} listings via {source}.")
    return listings


def main():
    config = load_json(CONFIG_PATH, {})
    if not config.get("url"):
        log("No URL configured in config.json")
        return 1

    topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    server = os.environ.get("NTFY_SERVER") or config.get("ntfy_server", "https://ntfy.sh")
    if not topic:
        log("No ntfy topic configured (set NTFY_TOPIC or config.ntfy_topic).")
        return 1

    state = load_json(STATE_PATH, {"seeded": False, "ids": []})
    seen = set(state.get("ids", []))
    seeded = bool(state.get("seeded"))

    listings = scrape(config)
    current_ids = [l["id"] for l in listings]

    if not listings:
        log("No listings extracted — leaving state untouched (see debug/ artifacts).")
        return 0

    new_listings = [l for l in listings if l["id"] not in seen]
    log(f"{len(new_listings)} new vs. last run.")

    notify_first = config.get("notify_on_first_run", False)
    cap = int(config.get("max_notifications_per_run", 15))

    if not seeded and not notify_first:
        log("First run: recording current listings as baseline WITHOUT notifying "
            "(set config.notify_on_first_run=true to change this).")
    else:
        to_send = new_listings[:cap]
        if len(new_listings) > cap:
            log(f"Capping notifications at {cap} (had {len(new_listings)}).")
        for l in to_send:
            try:
                send_ntfy(server, topic, l)
                log(f"Notified: {l['title']} ({l['id']})")
            except Exception as e:
                log(f"Failed to notify for {l['id']}: {e}")

    # Persist the union so listings that briefly drop off the page don't re-alert.
    merged = sorted(seen | set(current_ids))
    # Keep the state file from growing unbounded.
    MAX_KEEP = 2000
    if len(merged) > MAX_KEEP:
        merged = merged[-MAX_KEEP:]
    STATE_PATH.write_text(
        json.dumps({"seeded": True, "ids": merged}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"State saved: {len(merged)} known ids.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
