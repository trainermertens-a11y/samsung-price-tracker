#!/usr/bin/env python3
"""Collect explicit SKU-level EUR offers. Never infer variant prices from page order."""
import datetime
import html
import json
import pathlib
import re
from collections import deque
from urllib.parse import urljoin, urlsplit
from playwright.sync_api import sync_playwright

BASE = 'https://www.samsung.com'
OUT = pathlib.Path(__file__).parent / 'docs'
DEBUG = pathlib.Path(__file__).parent / 'debug'
SOURCES = {
    'Smartphone': BASE + '/be/smartphones/all-smartphones/',
    'Tablet': BASE + '/be/tablets/all-tablets/',
    'Watch': BASE + '/be/watches/all-watches/',
    'Buds': BASE + '/be/audio-sound/all-audio-sound/',
    'Galaxy Book': BASE + '/be/computers/all-computers/',
}
SEEDS = [
    BASE + '/be/smartphones/',
    BASE + '/be/smartphones/galaxy-s26/buy/',
    BASE + '/be/smartphones/galaxy-s/galaxy-s26-fe-graphite-128gb-sm-s741bzkdeub/',
    BASE + '/be/smartphones/galaxy-s/galaxy-s26-fe-blueberry-256gb-sm-s741bzvgeub/',
]
MAX_PAGES = 600
SKU_RE = re.compile(r'(?:sm|np)-[a-z0-9-]+', re.I)


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def category(url):
    path = urlsplit(url).path
    for part, name in [('smartphones', 'Smartphone'), ('tablets', 'Tablet'), ('watches', 'Watch'), ('audio-sound', 'Buds'), ('computers', 'Galaxy Book')]:
        if '/be/' + part + '/' in path:
            return name
    return None


def allowed(value, source):
    if not isinstance(value, str):
        return None
    url = urljoin(source, value)
    bits = urlsplit(url)
    if bits.hostname not in ('www.samsung.com', 'samsung.com') or not category(url):
        return None
    if '/business/' in bits.path or '/accessories/' in bits.path:
        return None
    if 'galaxy' not in bits.path and url not in SOURCES.values():
        return None
    return BASE + bits.path.rstrip('/') + '/'


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def price_number(value):
    text = str(value).strip()
    if re.fullmatch(r'\d+(?:\.\d{1,2})?', text):
        return float(text)
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})*,\d{2}', text):
        return float(text.replace('.', '').replace(',', '.'))
    return None


def product_rows(blob, source, title, checked):
    rows = []
    for product in walk(blob):
        types = product.get('@type', [])
        types = types if isinstance(types, list) else [types]
        if 'Product' not in types:
            continue
        name = re.sub(r'\s*\(Online (?:Exclusive|Only)\)', '', clean(product.get('name')), flags=re.I)
        if 'galaxy' not in name.lower():
            continue
        sku = clean(product.get('sku') or product.get('mpn'))
        if not sku or not SKU_RE.fullmatch(sku):
            continue
        properties = product.get('additionalProperty') or []
        properties = properties if isinstance(properties, list) else [properties]
        specs = {clean(p.get('name')): clean(p.get('value')) for p in properties if isinstance(p, dict)}
        def prop(pattern):
            return next((v for k, v in specs.items() if re.search(pattern, k, re.I)), None)
        identity = clean(product.get('url'))
        same_page = sku.lower() in source.lower() or (identity and allowed(identity, source) == allowed(source, source))
        context = name + (' | ' + title + ' | ' + urlsplit(source).path if same_page else '')
        def find(pattern):
            match = re.search(pattern, context, re.I)
            return clean(match.group(1)) if match else None
        offers = product.get('offers') or []
        offers = offers if isinstance(offers, list) else [offers]
        for offer in offers:
            if not isinstance(offer, dict) or offer.get('@type') == 'AggregateOffer':
                continue
            if clean(offer.get('priceCurrency')).upper() != 'EUR':
                continue
            value = price_number(offer.get('price', ''))
            if value is None or value <= 0:
                continue
            offer_text = clean(offer.get('name')) + ' ' + clean(offer.get('description'))
            if re.search(r'trade.?in|inruil|/\s*mnd|per month|monthly|instalment', offer_text, re.I):
                continue
            target = allowed(offer.get('url') or identity or source, source)
            if not target:
                continue
            size = product.get('size')
            if isinstance(size, dict):
                size = size.get('name') or size.get('value')
            rows.append({
                'name': name, 'category': category(target), 'sku': sku.upper(),
                'storage': prop(r'^(?:storage|opslag|opslagcapaciteit)(?:\s|$)') or find(r'\b((?:64|128|256|512)\s*GB|[124]\s*TB)\b'),
                'ram': prop(r'^(?:ram|memory|geheugen)(?:\s|$)'),
                'processor': prop(r'processor|cpu|processornaam'),
                'size': clean(size) or prop(r'screen size|schermformaat|case size') or find(r'(\d+(?:[.,]\d+)?\s*(?:mm|inch))'),
                'connectivity': prop(r'connectivity|connectiviteit') or find(r'\b(Wi-Fi|WiFi|5G|LTE|Bluetooth)\b'),
                'colour': product.get('color') or prop(r'colou?r|kleur'),
                'price_eur': value, 'price_text': f'€ {value:.2f}',
                'price_kind': 'published_structured_offer',
                'offer_conditions': offer_text.strip() or None,
                'availability': offer.get('availability'),
                'price_valid_until': offer.get('priceValidUntil'),
                'url': target, 'source_url': source, 'checked_at': checked,
                'specifications': specs, 'variant_title': title if same_page else name,
                'missing_details': [key for key, val in [('ram', prop(r'^(?:ram|memory|geheugen)(?:\s|$)'))] if not val],
            })
    return rows


def scrape():
    DEBUG.mkdir(exist_ok=True)
    checked = datetime.datetime.now(datetime.timezone.utc).isoformat()
    queue = deque(list(SOURCES.values()) + SEEDS)
    queued, visited, rows, failures = set(queue), set(), {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale='nl-BE')
        page = context.new_page()
        payloads = []
        def capture(response):
            host = urlsplit(response.url).hostname or ''
            if not (host == 'samsung.com' or host.endswith('.samsung.com')):
                return
            if 'json' in response.headers.get('content-type', ''):
                try:
                    payloads.append(response.json())
                except Exception:
                    pass
        page.on('response', capture)
        while queue and len(visited) < MAX_PAGES:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            payloads.clear()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=90000)
                for label in ['Akkoord', 'Accepteren', 'Accept']:
                    try:
                        page.get_by_role('button', name=label, exact=True).click(timeout=1000)
                        break
                    except Exception:
                        pass
                page.wait_for_timeout(4000)
                for _ in range(25):
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    page.wait_for_timeout(800)
                    buttons = page.get_by_role('button', name=re.compile('Bekijk meer|Toon meer|Meer laden|Load more|Show more', re.I))
                    visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible() and buttons.nth(i).is_enabled()]
                    if not visible:
                        break
                    visible[0].click(timeout=5000)
                    page.wait_for_timeout(1500)
                title = page.title()
                blobs = list(payloads)
                for text in page.locator('script[type="application/ld+json"]').all_text_contents():
                    try:
                        blobs.append(json.loads(text))
                    except ValueError:
                        pass
                links = page.locator('a[href]').evaluate_all('(nodes) => nodes.map(a => a.href)')
                extracted = []
                for blob in blobs:
                    extracted.extend(product_rows(blob, url, title, checked))
                    links.extend(v for v in strings(blob) if '/be/' in v and len(v) < 1000)
                for row in extracted:
                    key = (row['sku'], row['price_eur'], row['url'])
                    if key not in rows or len(row['specifications']) > len(rows[key]['specifications']):
                        rows[key] = row
                for value in links:
                    target = allowed(value, url)
                    if target and target not in queued:
                        queued.add(target)
                        queue.append(target)
                print(f'{len(visited)} pages; {len(rows)} SKU offers; {url}')
                if not extracted and SKU_RE.search(url):
                    failures.append({'url': url, 'reason': 'No explicit SKU-level EUR Product offer; not guessed'})
            except Exception as error:
                failures.append({'url': url, 'reason': str(error)})
        browser.close()
    result = sorted(rows.values(), key=lambda r: (r['category'], r['name'], r['sku'], r['price_eur']))
    report = {'checked_at': checked, 'visited_pages': len(visited), 'unvisited_pages': len(queue), 'unresolved_pages': failures, 'counts': {cat: sum(r['category'] == cat for r in result) for cat in SOURCES}, 'coverage': 'discovered explicit offers only; not guaranteed exhaustive', 'release_year_filter': None}
    (DEBUG / 'coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if queue:
        raise SystemExit('Crawl limit reached; inspect debug/coverage.json before publishing.')
    missing = [cat for cat in SOURCES if not report['counts'][cat]]
    if missing or not any('s26 fe' in r['name'].lower() for r in result):
        raise SystemExit('Coverage incomplete: ' + ', '.join(missing) + '; S26 FE required. Inspect debug/coverage.json. Previous published data preserved.')
    return result, report


def save(products, report):
    OUT.mkdir(exist_ok=True)
    date = report['checked_at'][:10]
    record = {'date': date, 'sources': SOURCES, 'coverage': report, 'products': products}
    history_path = OUT / 'history.json'
    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else []
    history = [r for r in history if r.get('date') != date] + [record]
    fields = ['name', 'sku', 'storage', 'ram', 'processor', 'size', 'connectivity', 'colour', 'price_text', 'availability', 'offer_conditions', 'checked_at']
    sections = []
    for row in products:
        body = ''.join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(str(row.get(k) or "Not provided"))}</dd>' for k in fields)
        body += ''.join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>' for k, v in row['specifications'].items())
        sections.append(f'<section><h2>{html.escape(row["name"])} — {html.escape(row["sku"])}</h2><dl>{body}</dl><a href="{html.escape(row["url"], quote=True)}">Samsung source</a></section>')
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Galaxy SKU prices</title><style>body{font-family:Arial;max-width:1000px;margin:2rem auto;padding:1rem}section{border-bottom:1px solid #ccc}dt{font-weight:bold}dd{overflow-wrap:anywhere}</style><h1>Samsung Belgium Galaxy variant offers</h1><p>Checked ' + html.escape(report['checked_at']) + '. Discovered SKU offers, not guaranteed exhaustive. Missing specifications are not inferred. No release-year filter. Confirm availability and offer conditions on Samsung. Historic prices are not current offers.</p>' + ''.join(sections) + '</html>'
    (OUT / 'latest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'index.html').write_text(page, encoding='utf-8')


if __name__ == '__main__':
    products, report = scrape()
    save(products, report)
    print(f'Saved {len(products)} explicit SKU offers. Inspect coverage for unresolved pages.')
