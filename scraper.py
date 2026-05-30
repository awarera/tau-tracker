#!/usr/bin/env python3
"""
TAU Tracker — Daily Scraper
Runs via GitHub Actions daily at 08:00 Nairobi (EAT).

Three listing statuses:
  active   — auction countdown running, bidding open now
  upcoming — on sale soon, no active bidding yet
  ended    — auction completed

Outputs:
  data/listings.json        full dataset
  data/rates.json           today's exchange rates
  data/rates_history.json   rolling 30-day rate log
"""

import json, re, os, sys, time
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
DISCOVER_PAGES   = 15    # pages per run after initial (10 items/page)
INITIAL_PAGES    = 80    # first-ever run — deeper crawl to build dataset
REQUEST_DELAY    = 1.5   # seconds between detail fetches
MAX_RECHECK      = 60    # max active/upcoming listings to re-check per run

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT          = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(ROOT, 'data')
SEED_FILE     = os.path.join(DATA_DIR, 'seed_stknos.txt')
LISTINGS_FILE = os.path.join(DATA_DIR, 'listings.json')
RATES_FILE    = os.path.join(DATA_DIR, 'rates.json')
HIST_FILE     = os.path.join(DATA_DIR, 'rates_history.json')
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept':          'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def fetch_html(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    ✗ {e}")
        return None

def fetch_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': HEADERS['User-Agent']
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"    ✗ API {url}: {e}")
        return None

# ── JSON persistence ───────────────────────────────────────────────────────────
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

# ── Time-left parser ───────────────────────────────────────────────────────────
def parse_time_left(tlv):
    """
    Parse TAU time-remaining strings into (status, auction_ends_at ISO | None).
    Examples:
      '1day+ 06:35:01'              → active, ~1.27 days from now
      '2 day 09:05 until the end'   → active, ~2.38 days from now
      'On sale soon'                → upcoming, None
      'End'  /  ''                  → ended, None
    """
    s = (tlv or '').strip().lower()
    now = datetime.now(timezone.utc)

    if not s or s == 'end' or s == 'ended':
        return 'ended', None

    if 'soon' in s or 'not decided' in s or 'sale soon' in s:
        return 'upcoming', None

    # Look for digit-based countdown
    if re.search(r'\d', s):
        try:
            days = hours = mins = secs = 0
            dm = re.search(r'(\d+)\s*day', s)
            hm = re.search(r'(\d+):(\d+):(\d+)', s)  # hh:mm:ss
            hm2 = re.search(r'(\d+):(\d+)(?!\d)', s)  # hh:mm only
            if dm:  days  = int(dm.group(1))
            if hm:  hours, mins, secs = int(hm.group(1)), int(hm.group(2)), int(hm.group(3))
            elif hm2: hours, mins = int(hm2.group(1)), int(hm2.group(2))
            delta = timedelta(days=days, hours=hours, minutes=mins, seconds=secs)
            ends_at = (now + delta).isoformat()
        except Exception:
            ends_at = None
        return 'active', ends_at

    return 'ended', None

# ── Discover stkNos from TAU search ───────────────────────────────────────────
def discover_stknos(max_pages):
    """
    Crawl TAU search results pages and extract stkNos.
    Tries the auction category first, then 'on sale soon'.
    """
    found = []
    seen  = set()

    # Category-filtered URLs (most relevant first)
    search_bases = [
        'https://www.tau-trade.com/sal_frt/stock/search?itemCategory=car&page={p}',
    ]

    for base in search_bases:
        print(f"  Source: {base.format(p='…')}")
        for page in range(1, max_pages + 1):
            html = fetch_html(base.format(p=page))
            if not html:
                print(f"  Page {page}: failed, stopping")
                break
            stks = re.findall(r'stkNo=([0-9a-f]{32})', html)
            added = sum(1 for s in stks if s not in seen and not seen.add(s) and found.append(s) is None)
            print(f"  Page {page}: {added} new  ({len(found)} total)")
            if added == 0 and page > 2:
                print("  No new stkNos — stopping early")
                break
            time.sleep(REQUEST_DELAY * 0.5)

    return found

# ── Load seed file ─────────────────────────────────────────────────────────────
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

# ── Parse TAU detail page ──────────────────────────────────────────────────────
def tv(html, *labels):
    """Extract first matching table cell by header label(s)."""
    for label in labels:
        for pat in [
            rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>',
            rf'<td[^>]*>\s*{re.escape(label)}\s*</td>\s*<td[^>]*>(.*?)</td>',
        ]:
            m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            if m:
                return re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
    return ''

def parse_listing(stk, html, existing=None):
    now_iso = datetime.now(timezone.utc).isoformat()
    d = existing.copy() if existing else {}
    d.update({
        'stk':        stk,
        'url':        f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}',
        'fetched_at': now_iso,
        'panels':     d.get('panels', {}),
        'airbags':    d.get('airbags', {}),
    })
    # Set listed_at only on first fetch
    if 'listed_at' not in d:
        d['listed_at'] = now_iso

    # ── Price — anchor to "Current Price:" to avoid false matches ─────────────
    pm = re.search(r'Current\s*Price[^<]*?([\d,]+)\s*Yen', html, re.IGNORECASE)
    if not pm:
        pm = re.search(r'([\d,]+)\s*Yen', html)
    d['price'] = int(pm.group(1).replace(',', '')) if pm else None

    # ── Bid count ──────────────────────────────────────────────────────────────
    bm = re.search(r'No\.\s*of\s*Bidder[^\d]*(\d+)', html, re.IGNORECASE)
    d['bids'] = int(bm.group(1)) if bm else 0

    # ── Status + auction end time ──────────────────────────────────────────────
    tl  = re.search(r'Time\s*left\b[^<]*?(?:</[^>]+>)?\s*([^<\n]+)', html, re.IGNORECASE)
    tlv = tl.group(1).strip() if tl else ''
    new_status, ends_at = parse_time_left(tlv)

    prev_status = d.get('status', '')
    d['status'] = new_status
    if ends_at:
        d['auction_ends_at'] = ends_at
    # Record when a listing transitions to ended
    if new_status == 'ended' and prev_status in ('active', 'upcoming') and 'ended_at' not in d:
        d['ended_at'] = now_iso

    # ── Basic info ─────────────────────────────────────────────────────────────
    d['sno']   = re.sub(r'No\.', '', tv(html, 'Stock No.')).strip()
    d['make']  = tv(html, 'Maker', 'Make') or d.get('make', '')
    d['model'] = tv(html, 'Model')         or d.get('model', '')
    d['grade'] = tv(html, 'Grade')         or d.get('grade', '')
    d['body']  = tv(html, 'Body Type')     or d.get('body', '')

    yr_raw = tv(html, 'First Registration', 'Year') or ''
    ym = re.search(r'(\d{4})', yr_raw)
    if ym: d['year'] = int(ym.group(1))

    km_raw = tv(html, 'Mileage') or ''
    km = re.search(r'([\d,]+)\s*km', km_raw, re.IGNORECASE)
    if km: d['mileage'] = int(km.group(1).replace(',', ''))

    d['col'] = tv(html, 'Color', 'Colour') or d.get('col', '')
    d['drv'] = tv(html, 'Drive System', 'Drive') or d.get('drv', '')
    d['tx']  = tv(html, 'Transmission')   or d.get('tx', '')
    d['eng'] = tv(html, 'Engine Type')    or d.get('eng', '')
    d['fuel']= tv(html, 'Fuel')           or d.get('fuel', '')
    d['loc'] = tv(html, 'Location', 'Due in Place') or d.get('loc', '')

    cc_raw = tv(html, 'Displacement') or ''
    cc_m = re.search(r'([\d,]+)', cc_raw)
    if cc_m: d['cc'] = int(cc_m.group(1).replace(',', ''))

    cap_raw = tv(html, 'Capacity') or ''
    if cap_raw.strip().isdigit(): d['cap'] = int(cap_raw.strip())

    # ── Damage info ────────────────────────────────────────────────────────────
    d['damage']    = tv(html, 'Area of Damage')              or d.get('damage', '')
    d['dc']        = tv(html, 'Drive Condition')             or d.get('dc', 'Unknown')
    d['engine_s']  = tv(html, 'Engine (time of assessment)', 'Engine') or d.get('engine_s', '')
    d['radiator']  = tv(html, 'Radiator & Condenser', 'Radiator') or d.get('radiator', '-')
    d['shift']     = tv(html, 'Shift Lever')                 or d.get('shift', '-')
    d['trans_oil'] = tv(html, 'Transmission Oil Pan', 'Oil Pan') or d.get('trans_oil', '-')
    d['main_dmg']  = tv(html, 'Main Damage')                 or d.get('main_dmg', '')

    # ── Remarks ────────────────────────────────────────────────────────────────
    rmk_m = re.search(
        r'Remarks?\s*:?\s*</(?:th|td|strong|b)[^>]*>\s*<(?:td|dd)[^>]*>(.*?)</(?:td|dd)>',
        html, re.IGNORECASE | re.DOTALL
    )
    if not rmk_m:
        rmk_m = re.search(r'Remarks?\s*[:\n](.*?)(?=\n\n|\Z)', html, re.IGNORECASE | re.DOTALL)
    if rmk_m:
        rmk = re.sub(r'<[^>]+>', ' ', rmk_m.group(1)).strip()
        rmk = re.sub(r'\s+', ' ', rmk)[:500]
        if len(rmk) > 5:
            d['rmk'] = rmk
    if 'rmk' not in d:
        d['rmk'] = ''

    # ── Airbags ────────────────────────────────────────────────────────────────
    ab_sec = re.search(r'[Aa]ir.?bag(.*?)(?=</table>|<h\d)', html, re.DOTALL)
    if ab_sec and 'finished' in ab_sec.group(1).lower():
        d['airbags'] = {'drv': 'Finished', 'pass': 'Finished'}

    # ── Category (CBM / parts lookup key) ─────────────────────────────────────
    bl = (d.get('body') or '').lower()
    cc = d.get('cc', 0)
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
        d['cat'] = d.get('cat', 'hatch')

    return d

# ── Exchange rates ─────────────────────────────────────────────────────────────
def fetch_rates():
    rates = {'jpu': 0.0064, 'usdKes': 130.0,
             'fetched_at': datetime.now(timezone.utc).isoformat()}

    data = fetch_json('https://api.frankfurter.app/latest?from=JPY&to=USD')
    if data and 'rates' in data:
        rates['jpu'] = round(data['rates']['USD'], 7)
        print(f"  JPY/USD : {rates['jpu']}")

    data = fetch_json('https://open.er-api.com/v6/latest/USD')
    if data and 'rates' in data and 'KES' in data['rates']:
        rates['usdKes'] = round(data['rates']['KES'], 2)
        print(f"  USD/KES : {rates['usdKes']}")
    else:
        data = fetch_json('https://api.exchangerate-api.com/v4/latest/USD')
        if data and 'rates' in data and 'KES' in data['rates']:
            rates['usdKes'] = round(data['rates']['KES'], 2)
            print(f"  USD/KES : {rates['usdKes']} (fallback)")

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

    listings  = load_json(LISTINGS_FILE, [])
    by_stk    = {l['stk']: l for l in listings}
    print(f"Dataset       : {len(listings)} listings  "
          f"({sum(1 for l in listings if l.get('status')=='active')} active  "
          f"{sum(1 for l in listings if l.get('status')=='upcoming')} upcoming  "
          f"{sum(1 for l in listings if l.get('status')=='ended')} ended)")

    pages = INITIAL_PAGES if len(listings) < 50 else DISCOVER_PAGES
    print(f"\n── Discovery ({pages} pages) ──")
    discovered = discover_stknos(pages)

    seed = load_seed()
    print(f"Seed file     : {len(seed)}")

    all_candidates = list(dict.fromkeys(discovered + seed))
    new_stks = [s for s in all_candidates if s not in by_stk]
    print(f"New to fetch  : {len(new_stks)}")

    # Fetch new listings
    print(f"\n── Fetching {len(new_stks)} new listing(s) ──")
    fetched = failed = 0
    for stk in new_stks:
        url = f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}'
        print(f"  {stk[:12]}… ", end='', flush=True)
        html = fetch_html(url)
        if html:
            d = parse_listing(stk, html)
            by_stk[stk] = d
            fetched += 1
            p = f"¥{d['price']:,}" if d.get('price') else '—'
            print(f"✓ {d.get('make',''):<8} {d.get('model',''):<14} "
                  f"{p:<12} {d.get('bids',0):>3}b  [{d['status']}]")
        else:
            failed += 1
            print('✗')
        time.sleep(REQUEST_DELAY)

    # Re-check active + upcoming listings
    to_recheck = [l for l in by_stk.values()
                  if l.get('status') in ('active', 'upcoming')][:MAX_RECHECK]
    if to_recheck:
        print(f"\n── Re-checking {len(to_recheck)} live/upcoming listing(s) ──")
        for listing in to_recheck:
            html = fetch_html(listing['url'])
            if html:
                prev_bids   = listing.get('bids', 0)
                prev_status = listing.get('status', '')
                updated = parse_listing(listing['stk'], html, existing=listing)
                by_stk[listing['stk']] = updated
                delta  = f" (+{updated['bids']-prev_bids})" if updated['bids'] > prev_bids else ''
                change = f" → {updated['status']}" if updated['status'] != prev_status else ''
                print(f"  ↻ {updated.get('make',''):<8} {updated.get('model',''):<12} "
                      f"{updated['bids']:>3}b{delta}{change}")
            time.sleep(REQUEST_DELAY * 0.7)

    # Save
    all_listings = list(by_stk.values())
    save_json(LISTINGS_FILE, all_listings)
    n_active   = sum(1 for l in all_listings if l.get('status') == 'active')
    n_upcoming = sum(1 for l in all_listings if l.get('status') == 'upcoming')
    n_ended    = sum(1 for l in all_listings if l.get('status') == 'ended')
    print(f"\nSaved {len(all_listings)}  "
          f"({fetched} new  {failed} failed  "
          f"| {n_active} active  {n_upcoming} upcoming  {n_ended} ended)")

    print(f"\n── Rates ──")
    rates = fetch_rates()
    save_json(RATES_FILE, rates)
    update_history(rates)
    print(f"  ¥1 = KES {round(rates['jpu']*rates['usdKes'],4):.4f}")

    print('\n✓  Done')
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
