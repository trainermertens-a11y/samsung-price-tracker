#!/usr/bin/env python3
"""
Daily price tracker for Samsung Belgium smartphones.

1. Opens https://www.samsung.com/be/smartphones/all-smartphones/ in a headless browser
   (the prices are loaded by JavaScript, so a plain HTTP request returns no prices).
2. Extracts product name, price and link.
3. Saves latest.json, appends to history.json and writes index.html for Chatbase.

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

# Matches prices like "€ 1.099,00", "€899", "€ 699,-"
PRICE_RE = re.compile(r"€\s?(\d{1,3}(?:\.\d{3})*(?:,\d{2}|,-)?|\d+(?:,\d{2})?)")


def to_float(price_text: str) -> float | None:
    """Convert Belgian price text (1.099,00) to a number (1099.0)."""
    t = price_text.replace(".", "").replace(",-", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def scrape() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="nl-BE",
        )
        page = context.new_page()
        # Wait for the HTML to load, not for "network idle" (Samsung's page never goes quiet).
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)

        # Close the cookie / country popup if it appears.
        for label in ["Akkoord", "Accepteren", "Accept", "OK"]:
            try:
                page.get_by_role("button", name=label).click(timeout=2_000)
                break
            except Exception:
                pass

        # Wait until product links appear (prices are loaded by JavaScript).
        try:
            page.wait_for_selector('a[href*="/smartphones/"]', timeout=60_000)
        except Exception:
            page.screenshot(path=str(OUT / "debug.png"), full_page=True)
            browser.close()
            raise SystemExit("Product links did not appear. Screenshot saved to docs/debug.png.")
        page.wait_for_timeout(3_000)  # short pause so prices finish loading

        # Collect every link that points to a smartphone product page, with its visible text.
        cards = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/smartphones/"]'))
                     .map(a => ({href: a.href, text: (a.innerText || '').trim()}))"""
        )
        browser.close()

    items, seen = [], set()
    for card in cards:
        match = PRICE_RE.search(card["text"])
        if not match or card["href"] in seen:
            continue
        seen.add(card["href"])
        name = card["text"].splitlines()[0].strip()
        items.append({
            "name": name,
            "price_text": "€ " + match.group(1),
            "price_eur": to_float(match.group(1)),
            "url": card["href"],
        })
    return items


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
        raise SystemExit("No prices found. The page layout may have changed; check the selectors in scrape().")
    save(products)
    print(f"Saved {len(products)} products to {OUT}")
