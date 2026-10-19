#!/usr/bin/env python3
"""
Daily price tracker for current-year Samsung Galaxy devices in Belgium.

Covers Galaxy smartphones, tablets, watches, Buds, and Galaxy Books.
Removes duplicate "(Online Exclusive)" listings and keeps devices first
listed by this tracker during the current calendar year.

Run:  python3 scrape_prices.py
"""
import json
import re
import datetime
import pathlib
import html
from playwright.sync_api import sync_playwright

BASE = "https://www.samsung.com/be"
CATEGORIES = {
    "Smartphone": f"{BASE}/smartphones/all-smartphones/",
    "Tablet": f"{BASE}/tablets/all-tablets/",
    "Watch": f"{BASE}/watches/all-watches/",
    "Buds": f"{BASE}/audio/all-audio/",
    "Galaxy Book": f"{BASE}/computers/all-computers/",
}
OUT = pathlib.Path(__file__).parent / "docs"
OUT.mkdir(exist_ok=True)
FIRST_SEEN_PATH = OUT / "device_first_seen.json"

PRICE_LINE_RE = re.compile(r"^€?\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2}|,-)$")


def to_float(price_text: str) -> float | None:
    t = price_text.replace("€", "").strip()
    t = t.replace(".", "").replace("-", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def find_url(name: str, links: list[str], fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    for href in links:
        if f"/{slug}/" in href.lower():
            return href
    return fallback


def parse_products(text: str, links: list[str], category: str, source_url: str) -> list[dict]:
    items, current_name, seen = [], None, set()

    for raw in text.splitlines():
        line = raw.strip()

        # Samsung repeats products as "(Online Exclusive)"; ignore these duplicates.
        if "(online exclusive)" in line.lower():
            continue

        if line.startswith("Galaxy "):
            current_name = line
            continue

        match = PRICE_LINE_RE.match(line)
        if match and current_name and current_name not in seen:
            seen.add(current_name)
            price_text = "€ " + line.lstrip("€ ").strip()
            items.append({
                "name": current_name,
                "category": category,
                "price_text": price_text,
                "price_eur": to_float(price_text),
                "url": find_url(current_name, links, source_url),
            })

    return items


def load_first_seen() -> dict[str, str]:
    if FIRST_SEEN_PATH.exists():
        return json.loads(FIRST_SEEN_PATH.read_text(encoding="utf-8"))
    return {}


def filter_current_year(items: list[dict], first_seen: dict[str, str], today: str) -> list[dict]:
    current_year = str(datetime.date.today().year)

    for item in items:
        first_seen.setdefault(item["name"], today)

    return [
        item for item in items
        if first_seen.get(item["name"], today)[:4] == current_year
    ]


def scrape_category(page, category: str, url: str) -> list[dict]:
    print(f"Scraping {category}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)

    for label in ["Akkoord", "Accepteren", "Accept", "OK"]:
        try:
            page.get_by_role("button", name=label).click(timeout=2_000)
            break
        except Exception:
            pass

    try:
        page.wait_for_function(r"() => /\d{2,4},-/.test(document.body.innerText)", timeout=60_000)
    except Exception:
        print(f"  No prices found for {category}; skipping.")
        return []

    for _ in range(15):
        more = page.get_by_role("button", name=re.compile("Bekijk meer"))
        if more.count() == 0 or not more.first.is_visible():
            break
        more.first.click()
        page.wait_for_timeout(2_500)

    page.wait_for_timeout(2_000)
    text = page.evaluate("() => document.body.innerText")
    links = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    return parse_products(text, links, category, url)


def scrape() -> list[dict]:
    items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="nl-BE",
        )
        page = context.new_page()

        for category, url in CATEGORIES.items():
            try:
                items.extend(scrape_category(page, category, url))
            except Exception as error:
                print(f"  Failed {category}: {error}")

        browser.close()

    # Remove exact duplicates while preserving category-specific entries.
    unique = {}
    for item in items:
        key = (item["name"], item["category"])
        unique[key] = item

    return sorted(unique.values(), key=lambda item: (item["category"], item["name"]))


def save(items: list[dict]) -> None:
    today = datetime.date.today().isoformat()
    first_seen = load_first_seen()
    items = filter_current_year(items, first_seen, today)
    record = {"date": today, "sources": CATEGORIES, "products": items}

    FIRST_SEEN_PATH.write_text(json.dumps(first_seen, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "latest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    history_path = OUT / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    history = [h for h in history if h["date"] != today] + [record]
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = "\n".join(
        f"<tr><td>{html.escape(p['category'])}</td><td>{html.escape(p['name'])}</td>"
        f"<td>{html.escape(p['price_text'])}</td>"
        f"<td><a href=\"{html.escape(p['url'])}\">View</a></td></tr>"
        for p in items
    )
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Samsung Galaxy device prices - Benelux</title>
<style>body{{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left}}</style>
</head><body>
<h1>Samsung Galaxy device prices - Benelux</h1>
<p>Current-year Galaxy smartphones, tablets, watches, Buds, and Galaxy Books listed on samsung.com/be on <strong>{today}</strong>.
Online-exclusive duplicate listings are excluded. Checkout prices may differ; always confirm on samsung.com/be.</p>
<table><tr><th>Category</th><th>Product</th><th>Price</th><th>Link</th></tr>
{rows}
</table></body></html>""", encoding="utf-8")


if __name__ == "__main__":
    products = scrape()
    if not products:
        raise SystemExit("No products could be read from any category page.")
    save(products)
    print(f"Saved {len(products)} current-year Galaxy products to {OUT}")
    for p in products:
        print(f"  [{p['category']}] {p['name']}: {p['price_text']}")
