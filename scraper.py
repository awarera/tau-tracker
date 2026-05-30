#!/usr/bin/env python3
"""
TAU Tracker — Daily Scraper
Runs via GitHub Actions on a daily schedule.

Outputs:
  data/listings.json       — all tracked listings
  data/rates.json          — today's exchange rates
  data/rates_history.json  — rolling 30-day rate history
"""

import json, re, os, time, sys
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT         = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(ROOT, 'data')
SEED_FILE    = os.path.join(DATA_DIR, 'seed_stknos.txt')
LISTINGS_FILE= os.path.join(DATA_DIR, 'listings.json')
RATES_FILE   = os.path.join(DATA_DIR, 'rates.json')
HIST_FILE    = os.path.join(DATA_DIR, 'rates_history.json')
os.makedirs(DATA_DIR, exist_ok=True)

# ── HTTP fetch ─────────────────────────────────────────────────────────────────
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def fetch(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    ✗ fetch error: {e}")
        return None

def fetch_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"    ✗ JSON fetch error ({url}): {e}")
        return None

# ── Parse TAU listing page ─────────────────────────────────────────────────────
def tval(html, label):
    """Extract a table cell value by its header label."""
    patterns = [
        rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>',
        rf'<td[^>]*class="[^"]*label[^"]*"[^>]*>\s*{re.escape(label)}\s*</td>\s*<td[^>]*>(.*?)</td>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
    return ''

def parse_listing(stk, html):
    d = {
        'stk': stk,
        'url': f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}',
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'panels': {},
        'airbags': {},
        'rmk': '',
    }

    # Price
    pm = re.search(r'([\d,]+)\s*[Yy]en', html)
    d['price'] = int(pm.group(1).replace(',', '')) if pm else None

    # Bids
    bm = re.search(r'(?:No\.\s*of\s*Bidder|Bidder\s*Count)[^\d]*(\d+)', html, re.IGNORECASE)
    if not bm:
        bm = re.search(r'bidder[^\d]*(\d+)', html, re.IGNORECASE)
    d['bids'] = int(bm.group(1)) if bm else 0

    # Status — check for time remaining
    tl = re.search(r'time\s*left[^<]*<[^>]+>([^<]+)', html, re.IGNORECASE)
    tlv = (tl.group(1).strip() if tl else '').lower()
    d['status'] = 'ended' if 'end' in tlv or not tlv else 'active'

    # Stock number
    sno_raw = tval(html, 'Stock No.')
    d['sno'] = re.sub(r'No\.', '', sno_raw).strip()

    # Basic specs
    d['make']  = tval(html, 'Maker') or tval(html, 'Make') or ''
    d['model'] = tval(html, 'Model') or ''
    d['grade'] = tval(html, 'Grade') or ''
    d['body']  = tval(html, 'Body Type') or ''

    yr_raw = tval(html, 'First Registration') or tval(html, 'Year') or ''
    ym = re.search(r'(\d{4})', yr_raw)
    d['year']  = int(ym.group(1)) if ym else None

    km_raw = tval(html, 'Mileage') or ''
    km = re.search(r'([\d,]+)\s*km', km_raw, re.IGNORECASE)
    d['mileage'] = int(km.group(1).replace(',', '')) if km else 0

    d['col']   = tval(html, 'Color') or tval(html, 'Colour') or ''
    d['drv']   = tval(html, 'Drive System') or tval(html, 'Drive') or ''
    d['tx']    = tval(html, 'Transmission') or ''

    cc_raw = tval(html, 'Displacement') or ''
    cc_m = re.search(r'([\d,]+)', cc_raw)
    d['cc']    = int(cc_m.group(1).replace(',', '')) if cc_m else 0

    d['eng']   = tval(html, 'Engine Type') or ''
    d['fuel']  = tval(html, 'Fuel') or ''
    d['loc']   = tval(html, 'Location') or ''

    cap_raw = tval(html, 'Capacity') or ''
    d['cap']   = int(cap_raw) if cap_raw.strip().isdigit() else 0

    # Damage fields
    d['damage']    = tval(html, 'Area of Damage') or ''
    d['dc']        = tval(html, 'Drive Condition') or 'Unknown'
    d['engine_s']  = tval(html, 'Engine (time of assessment)') or ''
    d['radiator']  = tval(html, 'Radiator & Condenser') or '-'
    d['shift']     = tval(html, 'Shift Lever') or '-'
    d['trans_oil'] = tval(html, 'Transmission Oil Pan') or '-'
    d['main_dmg']  = tval(html, 'Main Damage') or ''

    # Airbags: check for "Finished" keyword in airbag section
    ab_sec = re.search(r'airbag(.*?)(?=</table>|<h\d)', html, re.IGNORECASE | re.DOTALL)
    if ab_sec and 'finished' in ab_sec.group(1).lower():
        d['airbags'] = {'drv': 'Finished', 'pass': 'Finished'}

    # Category
    body_l = d['body'].lower()
    drv_l  = d['drv'].lower()
    cc     = d['cc']
    if 'kei' in body_l and ('rv' in body_l or 'suv' in body_l or 'jeep' in body_l):
        d['cat'] = 'kei-suv'
    elif 'kei' in body_l:
        d['cat'] = 'kei'
    elif any(k in body_l for k in ['cab', '1box', 'mv&', 'van', 'wagon']):
        d['cat'] = 'mpv'
    elif 'suv' in body_l or '4wd' in drv_l or '4x4' in drv_l:
        d['cat'] = 'large-suv' if cc >= 3000 else ('mid-suv' if cc >= 1800 else 'compact-suv')
    elif 'sedan' in body_l or 'saloon' in body_l:
        d['cat'] = 'sedan'
    else:
        d['cat'] = 'hatch'

    return d

# ── Load / save helpers ────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Load seed stkNos ──────────────────────────────────────────────────────────
def load_seed():
    if not os.path.exists(SEED_FILE):
        print(f"Seed file not found: {SEED_FILE}")
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

# ── Exchange rates ─────────────────────────────────────────────────────────────
def fetch_rates():
    rates = {
        'jpu': 0.0064,
        'usdKes': 130.0,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'source': 'fallback',
    }
    # JPY → USD  (Frankfurter — ECB data, free, no key)
    data = fetch_json('https://api.frankfurter.app/latest?from=JPY&to=USD')
    if data and 'rates' in data:
        rates['jpu'] = data['rates']['USD']
        rates['source'] = 'frankfurter'
        print(f"  JPY/USD: {rates['jpu']:.6f}")

    # USD → KES  (open.er-api.com — free, no key, has KES)
    data = fetch_json('https://open.er-api.com/v6/latest/USD')
    if data and 'rates' in data and 'KES' in data['rates']:
        rates['usdKes'] = round(data['rates']['KES'], 4)
        print(f"  USD/KES: {rates['usdKes']}")
    else:
        # Fallback: ExchangeRate-API (also free)
        data = fetch_json('https://api.exchangerate-api.com/v4/latest/USD')
        if data and 'rates' in data and 'KES' in data['rates']:
            rates['usdKes'] = round(data['rates']['KES'], 4)
            print(f"  USD/KES (fallback): {rates['usdKes']}")

    return rates

def update_history(rates):
    history = load_json(HIST_FILE, [])
    today   = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    history = [h for h in history if h.get('date') != today]
    history.append({'date': today, 'jpu': rates['jpu'], 'usdKes': rates['usdKes']})
    history = sorted(history, key=lambda x: x['date'])[-30:]
    save_json(HIST_FILE, history)
    print(f"  History: {len(history)} days")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"TAU Tracker Scraper — {ts}")
    print('=' * 56)

    # 1. Load existing listings
    listings    = load_json(LISTINGS_FILE, [])
    existing    = {l['stk'] for l in listings}
    print(f"Existing listings : {len(listings)}")

    # 2. Seed file
    seed = load_seed()
    print(f"Seed stkNos       : {len(seed)}")
    new_stks = [s for s in seed if s not in existing]
    print(f"New to fetch      : {len(new_stks)}")

    # 3. Fetch new listings from seed
    fetched = failed = 0
    for stk in new_stks:
        url = f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}'
        print(f"  → {stk[:12]}… ", end='', flush=True)
        html = fetch(url)
        if html:
            d = parse_listing(stk, html)
            listings.append(d)
            fetched += 1
            print(f"✓  {d.get('make','')} {d.get('model','')}  {('¥'+str(d['price'])) if d.get('price') else 'no price'}  {d.get('bids',0)} bids")
        else:
            failed += 1
        time.sleep(1.5)

    # 4. Re-check active listings for updated bids / status
    active = [l for l in listings if l.get('status') == 'active']
    if active:
        print(f"\nRe-checking {len(active)} active listings…")
        for listing in active:
            html = fetch(listing['url'])
            if html:
                updated = parse_listing(listing['stk'], html)
                for field in ['price', 'bids', 'status', 'fetched_at']:
                    listing[field] = updated.get(field, listing.get(field))
                print(f"  ↻ {listing.get('make','')} {listing.get('model','')} — {listing['bids']} bids  [{listing['status']}]")
            time.sleep(1.0)

    # 5. Save listings
    save_json(LISTINGS_FILE, listings)
    print(f"\nSaved {len(listings)} listings  ({fetched} new, {failed} failed)")

    # 6. Exchange rates
    print('\nFetching exchange rates…')
    rates = fetch_rates()
    save_json(RATES_FILE, rates)
    update_history(rates)

    print('\nDone ✓')
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
