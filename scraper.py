#!/usr/bin/env python3
"""
TAU Tracker — Daily Scraper
Runs via GitHub Actions daily at 08:00 Nairobi (EAT).

Statuses:
  active   — auction countdown running, bidding open
  upcoming — on sale soon, no bidding yet
  ended    — auction completed

price_source field:
  extracted       — found via label + largest-Yen heuristic (reliable)
  fallback        — found via Yen+USD pattern (less reliable)
  re-checked      — re-validated post-fix (no extractable price found)
  none            — no price found at all
"""

import json, re, os, sys, time
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
DISCOVER_PAGES   = 15     # pages per daily run (10 items/page)
INITIAL_PAGES    = 80     # first-ever run — deeper crawl
REQUEST_DELAY    = 1.5    # seconds between fetches
MAX_RECHECK      = 150    # active + upcoming re-checks per run (was 60)
MAX_REVALIDATE   = 150    # ended listings with unverified prices to re-validate per run

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

# ── HTTP ───────────────────────────────────────────────────────────────────────
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
            'Accept': 'application/json', 'User-Agent': HEADERS['User-Agent']
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
            with open(path) as f: return json.load(f)
        except Exception: pass
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def fJ(n):
    return f'¥{n:,}' if n else '—'

# ── Price extraction ───────────────────────────────────────────────────────────
def extract_price(html):
    """
    Extract bid/sale price from a TAU listing detail page.

    Root cause of previous failures: TAU pages have a "related listings"
    carousel near the top of the HTML (before the main content) that also
    uses "Current Price: X Yen" labels. Naively finding the first "current
    price" gives a related listing's price, not the main bid price.

    Fix: TAU's main price section ALWAYS contains "No. of Bidder" immediately
    after the price (even for 0-bid and on-sale-soon listings). The related
    carousel does NOT have this field. So we anchor to "No. of Bidder" and
    look back up to 500 chars — that window is guaranteed to contain only
    the main listing's price section.

    Returns (price: int|None, source: str)
    """
    # Strip all HTML tags once for clean text searching
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    tl   = text.lower()

    # ── Primary: anchor to "No. of Bidder" (main price section only) ──────────
    bid_idx = tl.find('no. of bidder')
    if bid_idx > 0:
        # Price label + value always appear before "No. of Bidder"
        section = text[max(0, bid_idx - 500):bid_idx + 50]
        for label in ['current price', 'suggested price', 'goods price', 'offer price']:
            lidx = section.lower().find(label)
            if lidx >= 0:
                chunk = section[lidx:lidx + 300]
                matches = re.findall(r'([\d,]{3,})\s*Yen', chunk, re.IGNORECASE)
                values  = [int(m.replace(',', '')) for m in matches
                           if int(m.replace(',', '')) >= 1000]
                if values:
                    return max(values), 'extracted'
        # Label not found but we're in the right section — take any Yen value
        matches = re.findall(r'([\d,]{3,})\s*Yen', section, re.IGNORECASE)
        values  = [int(m.replace(',', '')) for m in matches
                   if int(m.replace(',', '')) >= 1000]
        if values:
            return max(values), 'fallback'

    # ── Fallback: no "No. of Bidder" found (Tender / Stock items) ─────────────
    # Use label search but require USD notation to avoid related-item pollution
    for label in ['current price', 'suggested price', 'goods price', 'offer price']:
        idx = tl.find(label)
        if idx >= 0:
            chunk = text[idx:idx + 300]
            m = re.search(r'([\d,]{3,})\s*Yen[^)]{0,25}\(USD[\d,]+\)',
                          chunk, re.IGNORECASE)
            if m:
                val = int(m.group(1).replace(',', ''))
                if val >= 1000:
                    return val, 'extracted'

    # ── Last resort: any Yen+USD pattern on the page ──────────────────────────
    m = re.search(r'([\d,]{3,})\s*Yen[^)]{0,25}\(USD[\d,]+\)', text, re.IGNORECASE)
    if m:
        val = int(m.group(1).replace(',', ''))
        if val >= 1000:
            return val, 'fallback'

    return None, 'none'

# ── Time-left parser ───────────────────────────────────────────────────────────
def parse_time_left(html):
    """
    Returns (status, auction_ends_at_iso | None)
    active   = countdown found (digits + day/hr/colon pattern)
    upcoming = 'on sale soon' / 'not decided'
    ended    = 'End' / empty / no time pattern
    """
    tl_idx = html.lower().find('time left')
    tlv = ''
    if tl_idx >= 0:
        ctx = re.sub(r'<[^>]+>', ' ', html[tl_idx:tl_idx + 300])
        ctx = re.sub(r'\s+', ' ', ctx).strip()
        ctx = re.sub(r'^time left\s*[:\s]*', '', ctx, flags=re.IGNORECASE).strip()
        tlv = ctx.split('  ')[0].strip()[:80]

    s = tlv.lower()
    now = datetime.now(timezone.utc)

    if not s or s in ('end', 'ended', '-'):
        return 'ended', None
    if 'soon' in s or 'not decided' in s or 'sale soon' in s:
        return 'upcoming', None

    if re.search(r'\d+\s*(day|hr|:)', s, re.IGNORECASE):
        try:
            days = hours = mins = 0
            dm = re.search(r'(\d+)\s*day', s)
            hm = re.search(r'(\d+):(\d+):(\d+)', s)
            hm2 = re.search(r'(\d+):(\d+)(?!\d)', s)
            if dm:  days  = int(dm.group(1))
            if hm:  hours, mins = int(hm.group(1)), int(hm.group(2))
            elif hm2: hours, mins = int(hm2.group(1)), int(hm2.group(2))
            ends_at = (now + timedelta(days=days, hours=hours, minutes=mins)).isoformat()
        except Exception:
            ends_at = None
        return 'active', ends_at

    return 'ended', None

# ── Table value extractor ──────────────────────────────────────────────────────
def tv(html, *labels):
    """Extract table cell value. Handles icon images after label text."""
    for label in labels:
        esc = re.escape(label)
        for pat in [
            rf'<th[^>]*>[^<]*{esc}.*?</th>\s*<td[^>]*>(.*?)</td>',
            rf'<td[^>]*>[^<]*{esc}.*?</td>\s*<td[^>]*>(.*?)</td>',
        ]:
            m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            if m:
                val = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
                val = re.sub(r'\s+', ' ', val).strip()
                if val:
                    return val
    return ''

# ── Parse listing detail page ──────────────────────────────────────────────────
def parse_listing(stk, html, existing=None):
    now_iso = datetime.now(timezone.utc).isoformat()
    d = existing.copy() if existing else {}
    d.update({'stk': stk, 'url': f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}',
              'fetched_at': now_iso})

    if 'listed_at' not in d:
        d['listed_at'] = now_iso

    # ── Price ──────────────────────────────────────────────────────────────────
    price_val, price_source = extract_price(html)
    d['price']        = price_val
    d['price_source'] = price_source

    # ── Bids ──────────────────────────────────────────────────────────────────
    bm = re.search(r'No\.\s*of\s*Bidder[^\d]*(\d+)', html, re.IGNORECASE)
    d['bids'] = int(bm.group(1)) if bm else 0

    # ── Status ────────────────────────────────────────────────────────────────
    new_status, ends_at = parse_time_left(html)
    prev_status = d.get('status', '')
    d['status'] = new_status
    if ends_at:
        d['auction_ends_at'] = ends_at
    if new_status == 'ended' and prev_status in ('active', 'upcoming') and 'ended_at' not in d:
        d['ended_at'] = now_iso

    # ── Basic info ─────────────────────────────────────────────────────────────
    d['sno']   = re.sub(r'No\.', '', tv(html, 'Stock No.')).strip() or d.get('sno', '')
    d['make']  = tv(html, 'Maker', 'Make')   or d.get('make', '')
    d['model'] = tv(html, 'Model')           or d.get('model', '')
    d['grade'] = tv(html, 'Grade')           or d.get('grade', '')
    d['body']  = tv(html, 'Body Type')       or d.get('body', '')

    yr_raw = tv(html, 'First Registration', 'Year') or ''
    ym = re.search(r'(\d{4})', yr_raw)
    if ym: d['year'] = int(ym.group(1))

    km_raw = tv(html, 'Mileage') or ''
    km = re.search(r'([\d,]+)\s*km', km_raw, re.IGNORECASE)
    if km: d['mileage'] = int(km.group(1).replace(',', ''))

    d['col']  = tv(html, 'Color', 'Colour')      or d.get('col', '')
    d['drv']  = tv(html, 'Drive System', 'Drive') or d.get('drv', '')
    d['tx']   = tv(html, 'Transmission')          or d.get('tx', '')
    d['eng']  = tv(html, 'Engine Type')           or d.get('eng', '')
    d['fuel'] = tv(html, 'Fuel')                  or d.get('fuel', '')
    d['loc']  = tv(html, 'Location', 'Due in Place') or d.get('loc', '')

    cc_raw = tv(html, 'Displacement') or ''
    cc_m = re.search(r'([\d,]+)', cc_raw)
    if cc_m: d['cc'] = int(cc_m.group(1).replace(',', ''))

    cap_raw = tv(html, 'Capacity') or ''
    if cap_raw.strip().isdigit(): d['cap'] = int(cap_raw.strip())

    # ── Damage ────────────────────────────────────────────────────────────────
    d['damage']    = tv(html, 'Area of Damage')                        or d.get('damage', '')
    d['dc']        = tv(html, 'Drive Condition')                       or d.get('dc', 'Unknown')
    d['engine_s']  = tv(html, 'Engine (time of assessment)', 'Engine') or d.get('engine_s', '')
    d['radiator']  = tv(html, 'Radiator & Condenser', 'Radiator')     or d.get('radiator', '-')
    d['shift']     = tv(html, 'Shift Lever')                          or d.get('shift', '-')
    d['trans_oil'] = tv(html, 'Transmission Oil Pan', 'Oil Pan')      or d.get('trans_oil', '-')
    d['main_dmg']  = tv(html, 'Main Damage')                          or d.get('main_dmg', '')

    # ── Remarks — truncate at TAU's advertising section ───────────────────────
    rmk = ''
    rmk_m = re.search(
        r'Remarks?\s*:?\s*</[^>]+>\s*<[^>]+>(.*?)</[^>]+>',
        html, re.IGNORECASE | re.DOTALL
    )
    if not rmk_m:
        rmk_m = re.search(r'<th[^>]*>[^<]*Remarks?[^<]*</th>\s*<td[^>]*>(.*?)</td>',
                           html, re.IGNORECASE | re.DOTALL)
    if rmk_m:
        rmk = re.sub(r'<[^>]+>', ' ', rmk_m.group(1)).strip()
        rmk = re.sub(r'\s+', ' ', rmk)
    # Truncate TAU's "Related Products Used …" advertising section
    rp_idx = rmk.lower().find('related products used')
    if rp_idx > 0:
        rmk = rmk[:rp_idx].strip()
    d['rmk'] = rmk[:500] if rmk else d.get('rmk', '')

    # ── Airbags ───────────────────────────────────────────────────────────────
    ab_sec = re.search(r'[Aa]ir.?bag(.*?)(?=</table>|<h\d)', html, re.DOTALL)
    if ab_sec and 'finished' in ab_sec.group(1).lower():
        d['airbags'] = {'drv': 'Finished', 'pass': 'Finished'}
    elif 'airbags' not in d:
        d['airbags'] = {}

    if 'panels' not in d:
        d['panels'] = {}

    # ── Category (CBM lookup) ─────────────────────────────────────────────────
    bl = (d.get('body') or '').lower()
    cc = d.get('cc', 0)
    if 'kei' in bl and ('rv' in bl or 'suv' in bl or 'jeep' in bl):
        d['cat'] = 'kei-suv'
    elif 'kei' in bl:
        d['cat'] = 'kei'
    elif any(k in bl for k in ['cab', '1box', 'mv&', 'van', 'wagon']):
        d['cat'] = 'mpv'
    elif 'suv' in bl:
        d['cat'] = 'large-suv' if cc >= 3000 else ('mid-suv' if cc >= 1800 else 'compact-suv')
    elif any(k in bl for k in ['sedan', 'saloon', 'hardtop', 'coupe']):
        d['cat'] = 'sedan'
    else:
        d['cat'] = d.get('cat', 'hatch')

    return d

# ── Discovery ─────────────────────────────────────────────────────────────────
def discover_stknos(max_pages):
    found, seen = [], set()
    base = 'https://www.tau-trade.com/sal_frt/stock/search?itemCategory=car&page={p}'
    print(f"  Crawling up to {max_pages} search pages…")
    for page in range(1, max_pages + 1):
        html = fetch_html(base.format(p=page))
        if not html:
            print(f"  Page {page}: failed, stopping")
            break
        stks = re.findall(r'stkNo=([0-9a-f]{32})', html)
        added = 0
        for s in stks:
            if s not in seen:
                seen.add(s); found.append(s); added += 1
        print(f"  Page {page}: {added} new  ({len(found)} total)")
        if added == 0 and page > 2:
            print("  No new stkNos — stopping")
            break
        time.sleep(REQUEST_DELAY * 0.4)
    return found

def load_seed():
    if not os.path.exists(SEED_FILE): return []
    stks = []
    with open(SEED_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            m = re.search(r'[0-9a-f]{32}', line)
            if m: stks.append(m.group(0))
    return stks

# ── Exchange rates ─────────────────────────────────────────────────────────────
def fetch_rates():
    rates = {'jpu': 0.0064, 'usdKes': 130.0,
             'fetched_at': datetime.now(timezone.utc).isoformat()}
    data = fetch_json('https://api.frankfurter.app/latest?from=JPY&to=USD')
    if data and 'rates' in data:
        rates['jpu'] = round(data['rates']['USD'], 7)
        print(f"  JPY/USD : {rates['jpu']}")
    for api in ['https://open.er-api.com/v6/latest/USD',
                'https://api.exchangerate-api.com/v4/latest/USD']:
        data = fetch_json(api)
        if data and 'rates' in data and 'KES' in data['rates']:
            rates['usdKes'] = round(data['rates']['KES'], 2)
            print(f"  USD/KES : {rates['usdKes']}")
            break
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

    run_start      = time.monotonic()  # track wall time for safety valve
    SAFETY_MINUTES = 75                   # stop loops if approaching 90-min timeout
    listings  = load_json(LISTINGS_FILE, [])
    by_stk    = {l['stk']: l for l in listings}
    n_active  = sum(1 for l in listings if l.get('status') == 'active')
    n_upc     = sum(1 for l in listings if l.get('status') == 'upcoming')
    n_ended   = sum(1 for l in listings if l.get('status') == 'ended')
    print(f"Dataset       : {len(listings)}  ({n_active} active  {n_upc} upcoming  {n_ended} ended)")

    # ── 1. Discover new listings ───────────────────────────────────────────────
    pages = INITIAL_PAGES if len(listings) < 50 else DISCOVER_PAGES
    print(f"\n── Discovery ({pages} pages) ──")
    discovered = discover_stknos(pages)
    seed = load_seed()
    print(f"Seed file     : {len(seed)}")
    all_cands  = list(dict.fromkeys(discovered + seed))
    new_stks   = [s for s in all_cands if s not in by_stk]
    print(f"New to fetch  : {len(new_stks)}")

    # ── 2. Fetch new listings ──────────────────────────────────────────────────
    fetched = failed = 0
    if new_stks:
        print(f"\n── Fetching {len(new_stks)} new listing(s) ──")
    for stk in new_stks:
        url  = f'https://www.tau-trade.com/sal_frt/stock/detail?stkNo={stk}'
        print(f"  {stk[:12]}… ", end='', flush=True)
        html = fetch_html(url)
        if html:
            d = parse_listing(stk, html)
            by_stk[stk] = d
            fetched += 1
            p = fJ(d['price']) if d.get('price') else '—'
            print(f"✓ {d.get('make',''):<8} {d.get('model',''):<14} "
                  f"{p:<12} {d.get('bids',0):>3}b  [{d['status']}]  [{d['price_source']}]")
        else:
            failed += 1; print('✗')
        time.sleep(REQUEST_DELAY)

    # ── 3. Re-check active + upcoming listings ─────────────────────────────────
    # No cap — ALL active/upcoming re-checked every run (bid counts + prices change daily)
    to_recheck = [l for l in by_stk.values()
                  if l.get('status') in ('active', 'upcoming')]
    if to_recheck:
        print(f"\n── Re-checking {len(to_recheck)} active/upcoming listing(s) ──")
        RECHECK_DELAY  = REQUEST_DELAY * 0.4   # 0.6s — lighter than initial fetch
        checked = 0
        for listing in to_recheck:
            if (time.monotonic() - run_start) > SAFETY_MINUTES * 60:
                print(f"  ⚠ {SAFETY_MINUTES}m safety limit — stopping after {checked} re-checks")
                break
            html = fetch_html(listing['url'])
            if html:
                prev_bids   = listing.get('bids', 0)
                prev_status = listing.get('status', '')
                updated     = parse_listing(listing['stk'], html, existing=listing)
                by_stk[listing['stk']] = updated
                delta  = f" (+{updated['bids']-prev_bids})" if updated['bids'] > prev_bids else ''
                change = f" → {updated['status']}" if updated['status'] != prev_status else ''
                print(f"  ↻ {updated.get('make',''):<8} {updated.get('model',''):<12} "
                      f"{updated['bids']:>3}b{delta}{change}")
            checked += 1
            time.sleep(RECHECK_DELAY)

    # ── 4. Re-validate prices for ended listings with unverified prices ─────────
    # Targets listings where price_source is not 'extracted' (wrong/missing price)
    # Runs up to MAX_REVALIDATE per day until all historical prices are corrected
    to_reval = [l for l in by_stk.values()
                if l.get('status') == 'ended'
                and l.get('price_source', 'none') not in ('extracted', 're-checked')][:MAX_REVALIDATE]
    if to_reval:
        print(f"\n── Re-validating {len(to_reval)} price(s) for ended listings ──")
        rv_fixed = rv_failed = 0
        for listing in to_reval:
            if (time.monotonic() - run_start) > SAFETY_MINUTES * 60:
                print(f"  ⚠ Safety limit — stopping re-validation early")
                break
            html = fetch_html(listing['url'])
            if html:
                new_price, new_source = extract_price(html)
                if new_source == 'extracted' and new_price:
                    listing['price']        = new_price
                    listing['price_source'] = 'extracted'
                    rv_fixed += 1
                    print(f"  ✓ {listing.get('make',''):<8} {listing.get('model',''):<12} "
                          f"{fJ(new_price)}")
                else:
                    # Mark as re-checked so we don't attempt again needlessly
                    listing['price_source'] = 're-checked'
                    rv_failed += 1
            time.sleep(REQUEST_DELAY * 0.7)
        print(f"  Re-validated: {rv_fixed} prices corrected, {rv_failed} still unavailable")

    # ── 5. Save ────────────────────────────────────────────────────────────────
    all_listings = list(by_stk.values())
    save_json(LISTINGS_FILE, all_listings)
    n_a = sum(1 for l in all_listings if l.get('status') == 'active')
    n_u = sum(1 for l in all_listings if l.get('status') == 'upcoming')
    n_e = sum(1 for l in all_listings if l.get('status') == 'ended')
    n_p = sum(1 for l in all_listings if l.get('price_source') == 'extracted')
    print(f"\nSaved {len(all_listings)}  "
          f"({fetched} new  {failed} failed  | {n_a} active  {n_u} upcoming  {n_e} ended  "
          f"| {n_p} verified prices)")

    # ── 6. Exchange rates ──────────────────────────────────────────────────────
    print(f"\n── Rates ──")
    rates = fetch_rates()
    save_json(RATES_FILE, rates)
    update_history(rates)
    print(f"  ¥1 = KES {round(rates['jpu']*rates['usdKes'],4):.4f}")

    print('\n✓  Done')
    return 0  # always exit 0 — partial failures are expected

if __name__ == '__main__':
    sys.exit(main())
