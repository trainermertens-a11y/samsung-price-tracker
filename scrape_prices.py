#!/usr/bin/env python3
"""Galaxy price tracker. First-seen year is not a verified release year."""
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
PRICE_LINE_RE = re.compile(
    r"^(?:€\s*)?(?P<amount>\d{1,3}(?:[.\s]\d{3})+|\d+)"
    r"(?:,(?P<cents>\d{2}|-))?\s*(?:€)?$"
)


def to_float(price_text: str) -> float | None:
    text = price_text.replace("\u00a0", " " ).strip()
    match = PRICE_LINE_RE.fullmatch(text)
    if not match or ("€" not in text and "," not in text):
        return None
    amount = re.sub(r"[.\s]", "", match.group("amount"))
    cents = match.group("cents")
    return float(amount) + (int(cents) / 100 if cents and cents != "-" else 0)


def find_url(name: str, links: list[str], fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    for href in links:
        if f"/{slug}/" in href.lower():
            return href
    return fallback


def parse_products(text: str, links: list[str], category: str, source_url: str) -> list[dict]:
    items, current_name, seen = [], None, set()
    for raw in text.splitlines():
        line = raw.replace("\u00a0", " " ).strip()
        if not line:
            continue
        if line.startswith("Galaxy "):
            current_name = re.sub(
                r"\s*\(\s*online\s+(?:exclusive|only)\s*\)\s*",
                " ", line, flags=re.IGNORECASE
            ).strip()
            continue
        price = to_float(line)
        if price is None or current_name is None:
            continue
        if current_name not in seen:
            seen.add(current_name)
            price_text = "€ " + f"{price:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
            items.append({
                "name": current_name, "category": category,
                "price_text": price_text, "price_eur": price,
                "url": find_url(current_name, links, source_url),
            })
        current_name = None
    return items


def load_first_seen() -> dict[str, str]:
    if FIRST_SEEN_PATH.exists():
        return json.loads(FIRST_SEEN_PATH.read_text(encoding="utf-8"))
    return {}


def filter_current_year(items: list[dict], first_seen: dict[str, str], today: str) -> list[dict]:
    for item in items:
        first_seen.setdefault(item["name"], today)
    return [item for item in items if first_seen[item["name"]][:4] == today[:4]]


def scrape_category(page, category: str, url: str) -> list[dict]:
    print(f"Scraping {category}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    for label in ["Akkoord", "Accepteren", "Accept", "OK"]:
        try:
            page.get_by_role("button", name=label, exact=True).click(timeout=2_000)
            break
        except Exception:
            pass
    try:
        page.wait_for_function(
            r"() => /€\s*\d|\d[\d.\s]*,\s*(?:\d{2}|-)/.test(document.body?.innerText || '')",
            timeout=60_000
        )
    except Exception:
        print(f"WARNING: Price wait timed out for {category}; continuing extraction.")
    for _ in range(20):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1_000)
        buttons = page.get_by_role(
            "button", name=re.compile("Bekijk meer|Toon meer|Meer laden|Load more|Show more", re.IGNORECASE)
        )
        clicked = False
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                clicked = True
                break
        if not clicked:
            break
    page.wait_for_timeout(2_000)
    text = page.evaluate("() => document.body.innerText")
    links = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    items = parse_products(text, links, category, url)
    print(f"  {category}: extracted {len(items)} products")
    if not items:
        debug_dir = pathlib.Path(__file__).parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")
        (debug_dir / f"{slug}.txt").write_text(text, encoding="utf-8")
        print(f"WARNING: No {category} products extracted. Final URL: {page.url}")
    return items


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
                print(f"Failed {category}: {error}")
        browser.close()
    unique = {(item["name"], item["category"]): item for item in items}
    return sorted(unique.values(), key=lambda item: (item["category"], item["name"]))


def save(items: list[dict]) -> None:
    today = datetime.date.today().isoformat()
    first_seen = load_first_seen()
    items = filter_current_year(items, first_seen, today)
    missing = {"Smartphone", "Tablet", "Watch"} - {item["category"] for item in items}
    if missing:
        raise SystemExit("Refusing to publish incomplete results. Missing: " + ", ".join(sorted(missing)))
    record = {"date": today, "sources": CATEGORIES, "products": items}
    FIRST_SEEN_PATH.write_text(json.dumps(first_seen, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "latest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    history_path = OUT / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    history = [h for h in history if h["date"] != today] + [record]
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = "\n".join(
        f"<tr><td>{html.escape(p['category'])}</td><td>{html.escape(p['name'])}</td>"
        f"<td>{html.escape(p['price_text'])}</td><td><a href=\"{html.escape(p['url'])}\">View</a></td></tr>"
        for p in items
    )
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Samsung Galaxy device prices - Belgium</title>
<style>body{{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left}}</style>
</head><body><h1>Samsung Galaxy device prices - Belgium</h1>
<p>Prices collected on {today} from samsung.com/be. Names are normalised and duplicates removed.
Only devices first seen by this tracker this calendar year are included; this is not a verified release-year filter.
Checkout prices may differ. Confirm prices and offer conditions on Samsung's website.</p>
<table><tr><th>Category</th><th>Product</th><th>Price</th><th>Link</th></tr>
{rows}
</table></body></html>""", encoding="utf-8")


if __name__ == "__main__":
    products = scrape()
    if not products:
        raise SystemExit("No products could be read from any category page.")
    save(products)
    print("Saved validated category results.")
