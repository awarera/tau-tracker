#!/usr/bin/env python3
"""
TAU Tracker — Daily Scraper
Runs via GitHub Actions daily at 08:00 Nairobi time.

What it does each run:
  1. Auto-discovers new listings from TAU's search pages
  2. Reads seed_stknos.txt (your manually tracked listings)
  3. Fetches full details for any listing not yet in dataset
  4. Re-checks all active listings for updated bids / final status
  5. Fetches live JPY/USD and USD/KES exchange rates
  6. Saves everything back to data/ for the dashboard to read

Outputs:
  data/listings.json       — full listing dataset
  data/rates.json          — today's exchange rates
  data/rates_history.json  — rolling 30-day rate log
"""

import json, re, os, sys, time
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────
DISCOVER_PAGES   = 10    # search pages to crawl each run (10 items/page = 100 listings)
INITIAL_PAGES    = 50    # pages to crawl on first ever run (builds initial dataset)
REQUEST_DELAY    = 1.5   # seconds between fetches — be polite to TAU
MAX_RECHECK      = 50    # max active listings to re-check per run

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT          = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(ROOT, 'data')
SEED_FILE     = os.path.join(DATA_DIR, 'seed_stknos.txt')
LISTINGS_FILE = os.path.join(DATA_DIR, 'listings.json')
RATES_FILE    = os.path.join(DATA_DIR, 'rates.json')
HIST_FILE     = os.path.join(DATA_DIR, 'rates_history.json')
os.makedirs(DATA_DIR, exist_ok=True)

# ── HTTP ───────────────────────────────────────────────────────────────────────
HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0.0.0 Safari/537.36',
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def fetch_html(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    ✗ {e}")
        return None

def fetch_json_api(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json',
                                                    'User-Agent': HEADERS['User-Agent']})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"    ✗ API error {url}: {e}")
        return None

# ── JSON helpers ───────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Discover stkNos from TAU search pages ─────────────────────────────────────
def discover_stknos(max_pages):
    """
    Scrape TAU's passenger car search results and extract all stkNos.
    Returns an ordered list (newest first from TAU's default sort).
    """
    found = []
    seen  = set()
    base  = 'https://www.tau-trade.com/sal_frt/stock/search?itemCategory=car&page={page}'

    print(f"  Scanning {max_pages} search page(s)…")
    for page in range(1, max_pages + 1):
        url  = base.format(page=page)
        html = fetch_html(url)
        if not html:
            print(f"  Page {page}: fetch failed, stopping discovery")
            break

        stks = re.findall(r'stkNo=([0-9a-f]{32})', html)
        new_on_page = 0
        for stk in stks:
            if stk not in seen:
                seen.add(stk)
                found.append(stk)
                new_on_page += 1

        print(f"  Page {page}: {new_on_page} new stkNos ({len(found)} total)")

        # Stop early if nothing new on this page (already in our universe)
        if new_on_page == 0 and page > 1:
            print("  No new stkNos found — stopping discovery")
            break

        time.sleep(REQUEST_DELAY)

    return found

# ── Load seed stkNos (manually tracked) ───────────────────────────────────────
def load_seed():
    if not os.path.exists(SEED_FILE):
        return []
    stks = []
    with open(SEED_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.search(r'[0-9a-f]{32}', line)
            if m:
                stks.append(m.group(0))
    return stks

# ── Parse a TAU detail page ────────────────────────────────────────────────────
def tv(html, label):
    """Extract table cell value by header label."""
    for pat in [
        rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>',
        rf'<td[^>]*class="[^"]*label[^"]*"[^>]*>\s*{re.escape(label)}\s*</td>\s*<td[^>]*>(.*?)</td>',
    ]:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
    return ''

def parse_listing(stk, html):
    d = {
        'stk':        stk,
        'url':        f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}',
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'panels':     {},
        'airbags':    {},
        'rmk':        '',
    }

    # ── Price ──────────────────────────────────────────────────────────────────
    pm = re.search(r'([\d,]+)\s*Yen', html)
    d['price'] = int(pm.group(1).replace(',', '')) if pm else None

    # ── Bid count ──────────────────────────────────────────────────────────────
    bm = re.search(r'No\.\s*of\s*Bidder[^\d]*(\d+)', html, re.IGNORECASE)
    d['bids'] = int(bm.group(1)) if bm else 0

    # ── Status ─────────────────────────────────────────────────────────────────
    # "2 day 09:05 until the end" → active   "End" → ended
    tl  = re.search(r'Time\s*left.*?</[^>]+>([^<]+)', html, re.IGNORECASE | re.DOTALL)
    tlv = (tl.group(1).strip() if tl else '').strip()
    # Active = has a real countdown (digits + day/colon pattern), or "on sale soon"
    d['status'] = 'active' if re.search(r'\d+\s*(day|hr|:)', tlv, re.IGNORECASE) \
                              or 'soon' in tlv.lower() \
                           else 'ended'

    # ── Basic info ─────────────────────────────────────────────────────────────
    d['sno']   = re.sub(r'No\.', '', tv(html, 'Stock No.')).strip()
    d['make']  = tv(html, 'Maker')  or tv(html, 'Make')  or ''
    d['model'] = tv(html, 'Model')  or ''
    d['grade'] = tv(html, 'Grade')  or ''
    d['body']  = tv(html, 'Body Type') or ''

    yr_raw = tv(html, 'First Registration') or tv(html, 'Year') or ''
    ym = re.search(r'(\d{4})', yr_raw)
    d['year'] = int(ym.group(1)) if ym else None

    km_raw = tv(html, 'Mileage') or ''
    km = re.search(r'([\d,]+)\s*km', km_raw, re.IGNORECASE)
    d['mileage'] = int(km.group(1).replace(',', '')) if km else 0

    d['col'] = tv(html, 'Color')   or tv(html, 'Colour') or ''
    d['drv'] = tv(html, 'Drive System') or tv(html, 'Drive') or ''
    d['tx']  = tv(html, 'Transmission') or ''

    cc_raw = tv(html, 'Displacement') or ''
    cc_m   = re.search(r'([\d,]+)', cc_raw)
    d['cc']  = int(cc_m.group(1).replace(',', '')) if cc_m else 0

    d['eng']  = tv(html, 'Engine Type') or ''
    d['fuel'] = tv(html, 'Fuel') or ''
    d['loc']  = tv(html, 'Location') or ''

    cap_raw = tv(html, 'Capacity') or ''
    d['cap'] = int(cap_raw.strip()) if cap_raw.strip().isdigit() else 0

    # ── Damage fields ──────────────────────────────────────────────────────────
    d['damage']    = tv(html, 'Area of Damage') or ''
    d['dc']        = tv(html, 'Drive Condition') or 'Unknown'
    d['engine_s']  = tv(html, 'Engine (time of assessment)') or ''
    d['radiator']  = tv(html, 'Radiator & Condenser') or '-'
    d['shift']     = tv(html, 'Shift Lever') or '-'
    d['trans_oil'] = tv(html, 'Transmission Oil Pan') or '-'
    d['main_dmg']  = tv(html, 'Main Damage') or ''

    # ── Airbags ────────────────────────────────────────────────────────────────
    ab_sec = re.search(r'[Aa]ir.?bag(.*?)(?=</table>|<h\d)', html, re.DOTALL)
    if ab_sec and 'finished' in ab_sec.group(1).lower():
        d['airbags'] = {'drv': 'Finished', 'pass': 'Finished'}

    # ── Category (for CBM / parts value lookup in dashboard) ──────────────────
    bl  = d['body'].lower()
    cc  = d['cc']
    if 'kei' in bl and ('rv' in bl or 'suv' in bl or 'jeep' in bl):
        d['cat'] = 'kei-suv'
    elif 'kei' in bl:
        d['cat'] = 'kei'
    elif any(k in bl for k in ['cab', '1box', 'mv&', 'van', 'bonnet', 'wagon']):
        d['cat'] = 'mpv'
    elif 'suv' in bl:
        d['cat'] = 'large-suv' if cc >= 3000 else ('mid-suv' if cc >= 1800 else 'compact-suv')
    elif any(k in bl for k in ['sedan', 'saloon', 'hardtop']):
        d['cat'] = 'sedan'
    else:
        d['cat'] = 'hatch'

    return d

# ── Exchange rates ─────────────────────────────────────────────────────────────
def fetch_rates():
    rates = {
        'jpu':        0.0064,
        'usdKes':     130.0,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }

    # JPY → USD  (Frankfurter — ECB, free, no key)
    data = fetch_json_api('https://api.frankfurter.app/latest?from=JPY&to=USD')
    if data and 'rates' in data:
        rates['jpu'] = round(data['rates']['USD'], 7)
        print(f"  JPY/USD : {rates['jpu']}")

    # USD → KES  (open.er-api.com — free, no key, covers KES)
    data = fetch_json_api('https://open.er-api.com/v6/latest/USD')
    if data and 'rates' in data and 'KES' in data['rates']:
        rates['usdKes'] = round(data['rates']['KES'], 2)
        print(f"  USD/KES : {rates['usdKes']}")
    else:
        # Fallback
        data = fetch_json_api('https://api.exchangerate-api.com/v4/latest/USD')
        if data and 'rates' in data and 'KES' in data['rates']:
            rates['usdKes'] = round(data['rates']['KES'], 2)
            print(f"  USD/KES : {rates['usdKes']} (fallback API)")

    return rates

def update_history(rates):
    history = load_json(HIST_FILE, [])
    today   = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    history = [h for h in history if h.get('date') != today]
    history.append({'date': today, 'jpu': rates['jpu'], 'usdKes': rates['usdKes']})
    history = sorted(history, key=lambda x: x['date'])[-30:]
    save_json(HIST_FILE, history)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"TAU Tracker  ·  {ts}")
    print('─' * 56)

    # 1. Load existing data
    listings = load_json(LISTINGS_FILE, [])
    existing = {l['stk'] for l in listings}
    print(f"Existing dataset  : {len(listings)} listings")

    # 2. Determine how many pages to crawl
    # First run (empty or tiny dataset) → deeper crawl
    pages = INITIAL_PAGES if len(listings) < 50 else DISCOVER_PAGES
    print(f"\n── Auto-discovery ({pages} page(s)) ──")
    discovered = discover_stknos(pages)

    # 3. Seed file (manually tracked URLs)
    seed = load_seed()
    print(f"\nSeed file         : {len(seed)} stkNos")

    # 4. Merge: discovered + seed, de-duped, new only
    all_candidates = list(dict.fromkeys(discovered + seed))   # preserve order, dedup
    new_stks = [s for s in all_candidates if s not in existing]
    print(f"New to fetch      : {len(new_stks)}")

    # 5. Fetch new listings
    print(f"\n── Fetching {len(new_stks)} new listing(s) ──")
    fetched = failed = 0
    for stk in new_stks:
        url = f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}'
        print(f"  → {stk[:12]}…  ", end='', flush=True)
        html = fetch_html(url)
        if html:
            d = parse_listing(stk, html)
            listings.append(d)
            fetched += 1
            p = ('¥' + f"{d['price']:,}") if d.get('price') else 'no price'
            print(f"✓  {d.get('make','')} {d.get('model',''):<12} {p:<14} "
                  f"{d.get('bids',0):>3} bids  [{d['status']}]")
        else:
            failed += 1
            print('✗  failed')
        time.sleep(REQUEST_DELAY)

    # 6. Re-check active listings for live bid updates
    active = [l for l in listings if l.get('status') == 'active']
    active = active[:MAX_RECHECK]   # cap to avoid very long runs
    if active:
        print(f"\n── Re-checking {len(active)} active listing(s) ──")
        for listing in active:
            html = fetch_html(listing['url'])
            if html:
                updated = parse_listing(listing['stk'], html)
                prev_bids   = listing.get('bids', 0)
                prev_status = listing.get('status', '')
                for f in ['price', 'bids', 'status', 'fetched_at']:
                    listing[f] = updated.get(f, listing.get(f))
                flag  = ' ← ended'  if listing['status'] == 'ended' else ''
                delta = f" (+{listing['bids']-prev_bids})" if listing['bids'] > prev_bids else ''
                print(f"  ↻ {listing.get('make','')} {listing.get('model',''):<12} "
                      f"{listing['bids']:>3} bids{delta}{flag}")
            time.sleep(REQUEST_DELAY * 0.7)

    # 7. Save listings
    save_json(LISTINGS_FILE, listings)
    total   = len(listings)
    n_live  = sum(1 for l in listings if l.get('status') == 'active')
    n_ended = total - n_live
    print(f"\nSaved  {total} listings  "
          f"({fetched} new · {failed} failed · {n_live} live · {n_ended} ended)")

    # 8. Exchange rates
    print(f"\n── Exchange rates ──")
    rates = fetch_rates()
    save_json(RATES_FILE, rates)
    update_history(rates)
    print(f"  ¥1 = KES {round(rates['jpu'] * rates['usdKes'], 4):.4f}")

    print('\n✓  Done')
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
