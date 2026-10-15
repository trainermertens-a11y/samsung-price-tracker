#!/usr/bin/env python3
"""
Daily price tracker for Samsung Belgium smartphones.

1. Opens https://www.samsung.com/be/smartphones/all-smartphones/ in a headless browser
   (prices are loaded by JavaScript, so a plain HTTP request returns no prices).
2. Reads the visible page text: each product name is followed by its price,
   for example "Galaxy Z Fold8" then "1999,-".
3. Matches each product to its link and saves latest.json, history.json and index.html.

Run:  python3 scrape_prices.py
"""
import json
import re
import datetime
import pathlib
import html
from playwright.sync_api import sync_playwright

URL = "https://www.samsung.com/be/smartphones/all-smartphones/"
OUT = pathlib.Path(__file__).parent / "docs"  # GitHub Pages serves this folder
OUT.mkdir(exist_ok=True)

# A price on its own line, e.g. "1999,-", "1.999,-", "€ 899,00"
PRICE_LINE_RE = re.compile(r"^€?\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2}|,-)$")


def to_float(price_text: str) -> float | None:
    """Convert Belgian price text (1.999,- or 1.099,00) to a number."""
    t = price_text.replace("€", "").strip()
    t = t.replace(".", "").replace(",-", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def find_url(name: str, links: list[str]) -> str:
    """Find the product link whose path contains the product's name, e.g. /galaxy-z-fold8/."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    for href in links:
        if f"/{slug}/" in href.lower() and "/smartphones/" in href.lower():
            return href
    return URL


def parse_products(text: str, links: list[str]) -> list[dict]:
    """Pair each price line with the most recent 'Galaxy ...' product name above it."""
    items, current_name, seen = [], None, set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Galaxy "):
            current_name = line
            continue
        match = PRICE_LINE_RE.match(line)
        if match and current_name and current_name not in seen:
            seen.add(current_name)
            price_text = "€ " + line.lstrip("€ ").strip()
            items.append({
                "name": current_name,
                "price_text": price_text,
                "price_eur": to_float(price_text),
                "url": find_url(current_name, links),
            })
    return items


def scrape() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="nl-BE",
        )
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)

        # Close the cookie / country popup if it appears.
        for label in ["Akkoord", "Accepteren", "Accept", "OK"]:
            try:
                page.get_by_role("button", name=label).click(timeout=2_000)
                break
            except Exception:
                pass

        # Wait until at least one price (e.g. "1999,-") is on the page.
        try:
            page.wait_for_function(r"() => /\d{3,4},-/.test(document.body.innerText)", timeout=60_000)
        except Exception:
            print("DEBUG final URL:", page.url)
            print("DEBUG page title:", page.title())
            body_text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 1500)")
            print("DEBUG visible text (first 1500 characters):")
            print(body_text)
            try:
                page.screenshot(path=str(OUT / "debug.png"), full_page=True)
            except Exception as shot_error:
                print("DEBUG screenshot failed:", shot_error)
            browser.close()
            raise SystemExit("No prices found on the page. See DEBUG lines above.")

        # The page shows a batch of products, then a "Bekijk meer" (show more) button.
        # Click it until all products are loaded.
        for _ in range(10):
            more = page.get_by_role("button", name=re.compile("Bekijk meer"))
            if more.count() == 0 or not more.first.is_visible():
                break
            more.first.click()
            page.wait_for_timeout(2_500)

        page.wait_for_timeout(2_000)  # short pause so all cards finish rendering
        text = page.evaluate("() => document.body.innerText")
        links = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
        browser.close()

    return parse_products(text, links)


def save(items: list[dict]) -> None:
    today = datetime.date.today().isoformat()
    record = {"date": today, "source": URL, "products": items}

    (OUT / "latest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    history_path = OUT / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    history = [h for h in history if h["date"] != today] + [record]  # one entry per day
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = "\n".join(
        f"<tr><td>{html.escape(p['name'])}</td><td>{html.escape(p['price_text'])}</td>"
        f"<td><a href=\"{html.escape(p['url'])}\">View</a></td></tr>"
        for p in items
    )
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Samsung Belgium smartphone prices</title>
<style>body{{font-family:Arial,sans-serif;max-width:800px;margin:2rem auto}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left}}</style>
</head><body>
<h1>Samsung smartphone prices - Belgium</h1>
<p>Prices as shown on <a href="{URL}">samsung.com/be</a> on <strong>{today}</strong>.
Checkout prices may differ. Always confirm on samsung.com/be.</p>
<table><tr><th>Product</th><th>Price</th><th>Link</th></tr>
{rows}
</table></body></html>""", encoding="utf-8")


if __name__ == "__main__":
    products = scrape()
    if not products:
        raise SystemExit("No products could be read from the page. Check parse_products().")
    save(products)
    print(f"Saved {len(products)} products to {OUT}")
    for p in products:
        print(f"  {p['name']}: {p['price_text']}")
