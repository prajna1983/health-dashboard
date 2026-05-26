#!/usr/bin/env python3
"""
Health Dashboard Auto-Updater
Reads Apple Health (via Health Auto Export iCloud files) + Strava API
→ regenerates dashboard → pushes to GitHub Pages.

ONE-TIME SETUP (do this before first run):
  1. Install "Health Auto Export - JSON+CSV" on your iPhone (App Store, free)
  2. In the app: Automations → New Automation → iCloud Drive, format=JSON,
     cadence=every 2-4 hours. Add metrics: vo2_max, body_mass, body_fat_percentage
     plus any others you want.
  3. Run the setup script: cd ~/health-dashboard && bash setup.sh
  4. Test: python3 ~/health-dashboard/auto_update.py --diagnose

DAILY OPERATION: Zero effort. Runs at midnight, reads iCloud exports from your
iPhone, updates the dashboard, pushes to GitHub Pages.
"""

import glob, json, os, sys, time, subprocess
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import urllib.request, urllib.parse

# ── Configuration ─────────────────────────────────────────────────────────────
DASHBOARD_DIR     = os.path.expanduser('~/health-dashboard')
HEALTH_EXPORT_DIR = os.path.expanduser(
    '~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/New Automation'
)
CACHE_FILE  = os.path.join(DASHBOARD_DIR, 'health_cache.json')
INDEX_HTML  = os.path.join(DASHBOARD_DIR, 'index.html')
LOG_FILE    = os.path.join(DASHBOARD_DIR, 'update.log')

STRAVA_CLIENT_ID     = '250561'
STRAVA_CLIENT_SECRET = '45e1159516739f03074b4dfaa5e77173df04adf6'
STRAVA_TOKEN_FILE    = os.path.expanduser('~/.strava_tokens.json')

# Metric names as exported by Health Auto Export app
# Add vo2_max, body_mass, body_fat_percentage in the app to fill gaps
HAE_METRICS = {
    'resting_hr':     'resting_heart_rate',
    'heart_rate':     'heart_rate',
    'steps':          'step_count',
    'active_energy':  'active_energy',
    'vo2_max':        'vo2_max',
    'spo2':           'blood_oxygen_saturation',
    'body_temp':      'body_temperature',
    'body_mass':      'body_mass',
    'body_fat':       'body_fat_percentage',
}


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ── Date / math helpers ───────────────────────────────────────────────────────
def parse_hae_date(date_str):
    """Parse '2026-05-26 11:18:00 +0800' → 'YYYY-MM-DD'."""
    return date_str[:10] if date_str else None
def _avg(vals):
    vals = [v for v in vals if v is not None and v == v]
    return sum(vals) / len(vals) if vals else None

def r1(x):  return round(x, 1) if x is not None else None
def r0(x):  return round(x)    if x is not None else None
def r2(x):  return round(x, 2) if x is not None else None


# ── Core Data timestamp helpers ───────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  APPLE HEALTH — iCLOUD READER (via Health Auto Export app)
# ══════════════════════════════════════════════════════════════════════════════

def _src_priority(src):
    s = src.lower()
    if 'ultrahuman' in s:  return 0
    if 'watch' in s:       return 1
    return 2  # iPhone or other

def diagnose_icloud_export():
    """Show what's in the iCloud export folder."""
    if not os.path.exists(HEALTH_EXPORT_DIR):
        log(f'  ✗ Export folder not found: {HEALTH_EXPORT_DIR}')
        log('    Check Health Auto Export app is set to sync to iCloud Drive')
        return
    files = sorted(glob.glob(os.path.join(HEALTH_EXPORT_DIR, 'HealthAutoExport-*.json')))
    log(f'  Found {len(files)} export files in {HEALTH_EXPORT_DIR}')
    if not files:
        log('  ✗ No files yet — open Health Auto Export on iPhone and tap Export Now')
        return
    log(f'  Date range: {os.path.basename(files[0])} → {os.path.basename(files[-1])}')
    # Show metrics in latest file
    with open(files[-1], encoding='utf-8') as f:
        data = json.load(f)
    metrics = data.get('data', {}).get('metrics', [])
    log(f'\n  Metrics in latest file ({os.path.basename(files[-1])}):\n')
    for m in metrics:
        sample = m['data'][0] if m.get('data') else {}
        log(f'    {m["name"]:40s} {len(m.get("data",[]))} records  '
            f'sample qty={sample.get("qty","")}  src={sample.get("source","")}')
    missing = [v for k, v in HAE_METRICS.items()
               if v not in {m['name'] for m in metrics}]
    if missing:
        log(f'\n  ⚠ Missing metrics (add in Health Auto Export app): {", ".join(missing)}')
    else:
        log('\n  ✓ All required metrics present')

def _load_all_hae_files(days=400):
    """Load and merge all Health Auto Export JSON files for the last N days."""
    if not os.path.exists(HEALTH_EXPORT_DIR):
        log(f'  ✗ Export folder not found: {HEALTH_EXPORT_DIR}')
        log('    Path: ' + HEALTH_EXPORT_DIR)
        return None

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    files  = sorted(glob.glob(os.path.join(HEALTH_EXPORT_DIR, 'HealthAutoExport-*.json')))
    files  = [f for f in files if os.path.basename(f) >= f'HealthAutoExport-{cutoff}.json']

    if not files:
        log('  ✗ No export files found — open Health Auto Export and tap Export Now')
        return None

    log(f'  Reading {len(files)} export files ({os.path.basename(files[0])} → {os.path.basename(files[-1])})')

    all_metrics = defaultdict(list)
    seen = defaultdict(set)  # deduplicate by (date, qty, source) per metric

    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                data = json.load(f)
            for metric in data.get('data', {}).get('metrics', []):
                name = metric['name']
                for r in metric.get('data', []):
                    key = (r.get('date',''), r.get('qty'), r.get('source',''))
                    if key not in seen[name]:
                        seen[name].add(key)
                        all_metrics[name].append(r)
        except Exception as e:
            log(f'  ⚠ Skipping {os.path.basename(fp)}: {e}')

    return all_metrics

def _to_raw(records, metric_name):
    """Convert HAE records → list of (date, qty, source) tuples."""
    return [
        (parse_hae_date(r.get('date', '')), float(r['qty']), r.get('source', ''))
        for r in records
        if r.get('date') and r.get('qty') is not None
    ]

# ── Metric processors ─────────────────────────────────────────────────────────

def proc_rhr(raw):
    """Daily resting HR. Source priority: Ultrahuman > Watch > iPhone."""
    by_date = defaultdict(list)
    for date, val, src in raw:
        if 28 < val < 150:
            by_date[date].append((_src_priority(src), val, src))
    result = []
    for date in sorted(by_date):
        entries = sorted(by_date[date])
        best_pri = entries[0][0]
        best_vals = [v for p, v, _ in entries if p == best_pri]
        best_src  = entries[0][2]
        result.append({'date': date, 'v': r1(_avg(best_vals)), 'src': best_src})
    return result

def proc_rhr_from_raw_hr(raw):
    """Fallback: daily min of raw heart rate readings as RHR proxy."""
    by_date_src = defaultdict(lambda: defaultdict(list))
    for date, val, src in raw:
        if 30 < val < 120:  # plausible resting range
            by_date_src[date][src].append(val)
    by_date = defaultdict(list)
    for date, by_src in by_date_src.items():
        for src, vals in by_src.items():
            by_date[date].append((_src_priority(src), min(vals), src))
    result = []
    for date in sorted(by_date):
        entries = sorted(by_date[date])
        best_pri = entries[0][0]
        best_vals = [v for p, v, _ in entries if p == best_pri]
        best_src  = entries[0][2]
        result.append({'date': date, 'v': r1(_avg(best_vals)), 'src': best_src})
    return result

def proc_steps(raw):
    """Daily steps, one authoritative source: Ultrahuman > Watch > iPhone."""
    by_date = defaultdict(lambda: defaultdict(list))
    for date, val, src in raw:
        if 0 <= val <= 60000:
            key = 'ultra' if 'ultrahuman' in src.lower() \
                else ('watch' if 'watch' in src.lower() else 'phone')
            by_date[date][key].append(val)
    result = []
    for date in sorted(by_date):
        d = by_date[date]
        if   'ultra' in d: val = sum(d['ultra'])
        elif 'watch' in d: val = sum(d['watch'])
        else:              val = sum(d.get('phone', [0]))
        result.append({'date': date, 'v': r0(val)})
    return result

def proc_vo2(raw):
    """Monthly average VO2 Max."""
    by_month = defaultdict(list)
    for date, val, _ in raw:
        if 20 < val < 90:
            by_month[date[:7]].append(val)
    return [{'month': m, 'avg': r1(_avg(v))} for m, v in sorted(by_month.items())]

def proc_spo2(raw):
    """Daily SpO2 %; converts decimal (0.97) to percent (97.0) if needed."""
    by_date = defaultdict(list)
    for date, val, _ in raw:
        if val < 2:
            val = val * 100
        if 70 < val <= 100:
            by_date[date].append(val)
    return [{'date': d, 'v': r1(_avg(v))} for d, v in sorted(by_date.items())]

def proc_temp(raw):
    """Daily body/wrist temperature (Ultrahuman writes ~33–36 °C)."""
    by_date = defaultdict(list)
    for date, val, _ in raw:
        if 25 < val < 42:
            by_date[date].append(val)
    return [{'date': d, 'v': r2(_avg(v))} for d, v in sorted(by_date.items())]

def proc_energy(raw):
    """Monthly average active energy from Apple Watch."""
    by_day = defaultdict(float)
    for date, val, src in raw:
        if val > 0 and 'watch' in src.lower():
            by_day[date] += val
    by_month = defaultdict(list)
    for date, val in by_day.items():
        if val > 0:
            by_month[date[:7]].append(val)
    return [{'month': m, 'avg': r0(_avg(v))} for m, v in sorted(by_month.items())]

def monthly_from_daily_steps(steps_daily):
    by_month = defaultdict(list)
    for s in steps_daily:
        by_month[s['date'][:7]].append(s['v'])
    return [{'month': m, 'avg': r0(_avg(v))} for m, v in sorted(by_month.items())]

# ── Main Apple Health reader ──────────────────────────────────────────────────

def read_apple_health(days=400):
    """Read all Apple Health metrics from iCloud export files. Returns dict or None."""
    all_metrics = _load_all_hae_files(days)
    if not all_metrics:
        return None

    out = {}

    # Resting Heart Rate (prefer dedicated resting_heart_rate; fall back to daily min of heart_rate)
    rhr_records = all_metrics.get('resting_heart_rate', [])
    if rhr_records:
        log(f'    resting_heart_rate: {len(rhr_records)} records')
        out['rhr_daily'] = proc_rhr(_to_raw(rhr_records, 'resting_heart_rate'))
    else:
        hr_records = all_metrics.get('heart_rate', [])
        log(f'    heart_rate (RHR fallback): {len(hr_records)} records')
        out['rhr_daily'] = proc_rhr_from_raw_hr(_to_raw(hr_records, 'heart_rate'))

    # Steps
    step_records = all_metrics.get('step_count', [])
    log(f'    step_count: {len(step_records)} records')
    out['steps_daily'] = proc_steps(_to_raw(step_records, 'step_count'))

    # VO2 Max
    vo2_records = all_metrics.get('vo2_max', [])
    log(f'    vo2_max: {len(vo2_records)} records' +
        (' ⚠ (add "vo2_max" in Health Auto Export)' if not vo2_records else ''))
    if vo2_records:
        out['monthly_vo2'] = proc_vo2(_to_raw(vo2_records, 'vo2_max'))

    # SpO2
    spo2_records = all_metrics.get('blood_oxygen_saturation', [])
    log(f'    blood_oxygen_saturation: {len(spo2_records)} records')
    out['spo2_daily'] = proc_spo2(_to_raw(spo2_records, 'blood_oxygen_saturation'))

    # Body Temperature (Ultrahuman wrist)
    temp_records = all_metrics.get('body_temperature', [])
    log(f'    body_temperature: {len(temp_records)} records')
    out['body_temp'] = proc_temp(_to_raw(temp_records, 'body_temperature'))

    # Active Energy
    energy_records = all_metrics.get('active_energy', [])
    log(f'    active_energy: {len(energy_records)} records')
    out['monthly_energy'] = proc_energy(_to_raw(energy_records, 'active_energy'))

    # Weight
    weight_records = all_metrics.get('body_mass', [])
    log(f'    body_mass: {len(weight_records)} records' +
        (' ⚠ (add "body_mass" in Health Auto Export)' if not weight_records else ''))
    if weight_records:
        sorted_w = sorted(weight_records, key=lambda r: r.get('date', ''))
        out['weight_current'] = r1(sorted_w[-1]['qty'])
        out['weight_first']   = r1(sorted_w[0]['qty'])

    # Body Fat
    fat_records = all_metrics.get('body_fat_percentage', [])
    log(f'    body_fat_percentage: {len(fat_records)} records')
    if fat_records:
        latest = sorted(fat_records, key=lambda r: r.get('date', ''))[-1]
        val = latest['qty']
        out['bodyfat_current'] = r1(val * 100 if val < 1 else val)

    log(f'  Summary: {len(out.get("rhr_daily",[]))} RHR days, '
        f'{len(out.get("steps_daily",[]))} step days, '
        f'{len(out.get("spo2_daily",[]))} SpO2 days, '
        f'{len(out.get("body_temp",[]))} temp days')
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  STRAVA
# ══════════════════════════════════════════════════════════════════════════════

def strava_get_token():
    tokens = {}
    if os.path.exists(STRAVA_TOKEN_FILE):
        with open(STRAVA_TOKEN_FILE) as f:
            tokens = json.load(f)

    if not tokens.get('access_token') or time.time() > tokens.get('expires_at', 0) - 300:
        log('  Refreshing Strava token...')
        data = urllib.parse.urlencode({
            'client_id':     STRAVA_CLIENT_ID,
            'client_secret': STRAVA_CLIENT_SECRET,
            'refresh_token': tokens.get('refresh_token', 'cfbe66279769088bc111fcb74da231ca327c50c3'),
            'grant_type':    'refresh_token'
        }).encode()
        req = urllib.request.Request('https://www.strava.com/oauth/token', data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        tokens.update({
            'access_token':  resp['access_token'],
            'refresh_token': resp['refresh_token'],
            'expires_at':    resp['expires_at']
        })
        with open(STRAVA_TOKEN_FILE, 'w') as f:
            json.dump(tokens, f)
        log(f'  Token valid for {resp["expires_in"] // 3600}h')

    return tokens['access_token']

def strava_fetch(days=400):
    """Fetch all Strava activities for the last N days."""
    try:
        token   = strava_get_token()
        after   = int(time.time()) - days * 86400
        acts, p = [], 1
        while True:
            url = (f'https://www.strava.com/api/v3/athlete/activities'
                   f'?per_page=100&page={p}&after={after}')
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, timeout=30) as r:
                batch = json.loads(r.read())
            if not batch:
                break
            acts.extend(batch)
            log(f'    page {p}: {len(batch)} activities')
            p += 1
            time.sleep(0.3)
        log(f'  Strava: {len(acts)} total activities fetched')
        return acts
    except Exception as e:
        log(f'  Strava fetch failed: {e}')
        return None

def process_strava(activities):
    """Convert raw Strava activities into dashboard data structures."""
    rides, runs, strength = [], [], []

    for a in activities:
        sport = a.get('sport_type') or a.get('type', '')
        date  = (a.get('start_date_local') or '')[:10]
        if not date:
            continue

        if sport in ('Ride', 'VirtualRide', 'GravelRide', 'MountainBikeRide'):
            dist = round(a.get('distance', 0) / 1000, 2)
            if dist < 1:
                continue
            rides.append({
                'date': date,
                'name': a.get('name', ''),
                'dist': dist,
                'elev': round(a.get('total_elevation_gain', 0)),
                'w':    r1(a.get('average_watts') or None),
                'hr':   r1(a.get('average_heartrate') or None),
                'prs':  a.get('achievement_count', 0)
            })
        elif sport in ('Run', 'TrailRun', 'VirtualRun'):
            dist = round(a.get('distance', 0) / 1000, 2)
            mt   = a.get('moving_time', 0)
            pace = r2(mt / 60 / dist) if dist > 0.5 else None
            runs.append({
                'date': date,
                'dist': dist,
                'pace': pace,
                'hr':   r1(a.get('average_heartrate') or None)
            })
        elif sport in ('WeightTraining', 'Workout', 'Crossfit', 'Yoga', 'Pilates', 'Elliptical'):
            strength.append({'date': date, 'sport': sport})

    rides.sort(key=lambda r: r['date'])

    # Weekly cycling
    weekly = {}
    for r in rides:
        dt  = datetime.strptime(r['date'], '%Y-%m-%d')
        mon = (dt - timedelta(days=dt.weekday())).strftime('%Y-%m-%d')
        if mon not in weekly:
            weekly[mon] = {'week': mon, 'dist': 0, 'rides': 0, 'elev': 0}
        weekly[mon]['dist'] = round(weekly[mon]['dist'] + r['dist'], 1)
        weekly[mon]['rides'] += 1
        weekly[mon]['elev']  += r['elev']

    # Monthly cycling
    monthly_c = {}
    for r in rides:
        m = r['date'][:7]
        if m not in monthly_c:
            monthly_c[m] = {'month': m, 'dist': 0, 'rides': 0, 'elev': 0, '_w': [], '_hr': []}
        monthly_c[m]['dist'] = round(monthly_c[m]['dist'] + r['dist'], 1)
        monthly_c[m]['rides'] += 1
        monthly_c[m]['elev']  += r['elev']
        if r['w'] and r['w'] > 20:  monthly_c[m]['_w'].append(r['w'])
        if r['hr'] and r['hr'] > 50: monthly_c[m]['_hr'].append(r['hr'])

    cyc_list = []
    for m, d in sorted(monthly_c.items()):
        cyc_list.append({'month': m, 'dist': d['dist'], 'rides': d['rides'], 'elev': d['elev'],
                         'avg_w': r1(_avg(d['_w'])), 'avg_hr': r1(_avg(d['_hr']))})

    # Monthly running
    monthly_r = {}
    for r in runs:
        m = r['date'][:7]
        if m not in monthly_r:
            monthly_r[m] = {'month': m, 'dist': 0, 'runs': 0, '_p': [], '_hr': []}
        monthly_r[m]['dist']  = round(monthly_r[m]['dist'] + r['dist'], 1)
        monthly_r[m]['runs'] += 1
        if r['pace'] and 3 < r['pace'] < 12:  monthly_r[m]['_p'].append(r['pace'])
        if r['hr'] and r['hr'] > 50:           monthly_r[m]['_hr'].append(r['hr'])

    run_list = [{'month': m, 'dist': d['dist'], 'runs': d['runs'],
                 'avg_pace': r2(_avg(d['_p'])), 'avg_hr': r1(_avg(d['_hr']))}
                for m, d in sorted(monthly_r.items())]

    # Monthly strength
    str_by_month = defaultdict(int)
    for s in strength:
        str_by_month[s['date'][:7]] += 1
    str_list = [{'month': m, 'sessions': n} for m, n in sorted(str_by_month.items())]

    # Power trend
    pwr_trend = [{'month': m['month'], 'w': m['avg_w']} for m in cyc_list if m['avg_w']]

    return {
        'rides':      rides,
        'weekly_cyc': sorted(weekly.values(), key=lambda x: x['week']),
        'monthly_cyc': cyc_list,
        'monthly_run': run_list,
        'monthly_str': str_list,
        'power_trend': pwr_trend,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CACHE — persists historical data across runs
# ══════════════════════════════════════════════════════════════════════════════

def _init_cache_from_html():
    """Seed cache from existing index.html on very first run."""
    if not os.path.exists(INDEX_HTML):
        return {}
    log('  Initialising cache from existing index.html...')
    with open(INDEX_HTML, encoding='utf-8') as f:
        html = f.read()
    start = html.find('const DATA=')
    if start == -1:
        return {}
    end = html.find('\n', start)
    try:
        data = json.loads(html[start + 11 : end].rstrip(';'))
        hk   = data.get('hk', {})
        cache = {
            'rhr_daily':      data.get('rhr_daily', []),
            'monthly_vo2':    data.get('monthly_vo2', []),
            'body_temp':      data.get('body_temp', []),
            'spo2_daily':     data.get('spo2_daily', []),
            'monthly_steps':  data.get('monthly_steps', []),
            'steps_daily':    [],
            'monthly_energy': data.get('monthly_energy', []),
            'rides':          data.get('rides', []),
            'weekly_cyc':     data.get('weekly_cyc', []),
            'monthly_cyc':    data.get('monthly_cyc', []),
            'monthly_run':    data.get('monthly_run', []),
            'monthly_str':    data.get('monthly_str', []),
            'power_trend':    data.get('power_trend', []),
            'weight_current': hk.get('weight_current'),
            'weight_first':   hk.get('weight_first'),
        }
        log(f'  Seeded cache: {len(cache["rhr_daily"])} RHR days, '
            f'{len(cache["rides"])} rides, {len(cache["monthly_vo2"])} VO2 months')
        return cache
    except Exception as e:
        log(f'  Cache init from HTML failed: {e}')
        return {}

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return _init_cache_from_html()

def merge_into_cache(cache, fresh_hk, fresh_sk):
    """
    Merge fresh data into cache. Fresh data wins for overlapping dates/months.
    Old data from the cache is preserved for dates not covered by fresh data.
    This ensures historical Apple Health XML data is never lost.
    """
    result = dict(cache)

    if fresh_hk:
        # Daily time-series: merge by date key
        for key, date_key in [('rhr_daily','date'), ('spo2_daily','date'),
                               ('body_temp','date'), ('steps_daily','date')]:
            fresh = fresh_hk.get(key, [])
            if fresh:
                merged = {r[date_key]: r for r in cache.get(key, [])}
                merged.update({r[date_key]: r for r in fresh})
                result[key] = sorted(merged.values(), key=lambda r: r[date_key])

        # Monthly time-series: merge by month key
        for key in ('monthly_vo2', 'monthly_energy'):
            fresh = fresh_hk.get(key, [])
            if fresh:
                merged = {r['month']: r for r in cache.get(key, [])}
                merged.update({r['month']: r for r in fresh})
                result[key] = sorted(merged.values(), key=lambda r: r['month'])

        # Scalar values
        for key in ('weight_current', 'weight_first', 'bodyfat_current'):
            if fresh_hk.get(key) is not None:
                result[key] = fresh_hk[key]

    if fresh_sk:
        # Strava: always use full fresh history (API returns 400 days)
        for key in ('rides', 'weekly_cyc', 'monthly_cyc', 'monthly_run',
                    'monthly_str', 'power_trend'):
            if fresh_sk.get(key) is not None:
                result[key] = fresh_sk[key]

    return result

def save_cache(data):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, separators=(',', ':'), ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
#  KPI COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_hk_kpis(data):
    now   = datetime.now()
    c30   = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    c60   = (now - timedelta(days=60)).strftime('%Y-%m-%d')
    c90   = (now - timedelta(days=90)).strftime('%Y-%m-%d')

    rhr       = data.get('rhr_daily', [])
    rhr_30d   = [r['v'] for r in rhr if r['date'] >= c30]
    rhr_prev  = [r['v'] for r in rhr if c90 <= r['date'] < c60]
    rhr_cur   = rhr[-1]['v'] if rhr else None
    rhr_30    = r1(_avg(rhr_30d))
    rhr_prev_ = r1(_avg(rhr_prev))
    rhr_chg   = r1(((rhr_30 / rhr_prev_) - 1) * 100) if rhr_30 and rhr_prev_ else None

    vo2 = data.get('monthly_vo2', [])
    vo2_cur  = vo2[-1]['avg'] if vo2 else None
    vo2_prev = vo2[-3]['avg'] if len(vo2) >= 3 else None

    temp   = data.get('body_temp', [])
    temp30 = [t['v'] for t in temp if t['date'] >= c30]

    spo2   = data.get('spo2_daily', [])
    spo30  = [s['v'] for s in spo2 if s['date'] >= c30]

    # Steps: prefer daily, fall back to monthly_steps cache
    sd = data.get('steps_daily', [])
    if sd:
        steps30 = [s['v'] for s in sd if s['date'] >= c30]
    else:
        ms = data.get('monthly_steps', [])
        steps30 = [m['avg'] for m in ms[-3:]] if ms else []

    return {
        'rhr_current':    rhr_cur,
        'rhr_avg_30d':    rhr_30,
        'rhr_avg_prev':   rhr_prev_,
        'rhr_change_pct': rhr_chg,
        'vo2_current':    vo2_cur,
        'vo2_prev':       vo2_prev,
        'weight_current': data.get('weight_current'),
        'weight_first':   data.get('weight_first'),
        'temp_avg_30d':   r2(_avg(temp30)),
        'spo2_avg_30d':   r1(_avg(spo30)),
        'steps_avg_30d':  r0(_avg(steps30)) if steps30 else None,
        'last_updated':   now.strftime('%Y-%m-%d %H:%M'),
    }

def compute_sk_kpis(data):
    rides = data.get('rides', [])
    c30 = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    c90 = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

    r30  = [r for r in rides if r['date'] >= c30]
    r90  = [r for r in rides if r['date'] >= c90]
    w30  = [r['w'] for r in r30 if r.get('w') and r['w'] > 20]
    wprev= [r['w'] for r in rides if c90 <= r['date'] < c30 and r.get('w') and r['w'] > 20]

    mrun = data.get('monthly_run', [])
    mstr = data.get('monthly_str', [])
    last_run = mrun[-1] if mrun else {}
    last_str = mstr[-1] if mstr else {}

    best_ride = max(rides, key=lambda r: r['dist'], default={})
    best_pwr  = max(rides, key=lambda r: r.get('w') or 0, default={})

    aw30  = r1(_avg(w30))
    awprv = r1(_avg(wprev))
    hr30  = [r['hr'] for r in r30 if r.get('hr') and r['hr'] > 80]

    return {
        'rides_30d':              len(r30),
        'dist_30d':               r1(sum(r['dist'] for r in r30)),
        'elev_30d':               r0(sum(r['elev'] for r in r30)),
        'rides_90d':              len(r90),
        'dist_90d':               r1(sum(r['dist'] for r in r90)),
        'avg_watts_30d':          aw30,
        'avg_watts_prev':         awprv,
        'watts_change':           r1((aw30 or 0) - (awprv or 0)),
        'best_ride_dist':         best_ride.get('dist'),
        'best_ride_name':         best_ride.get('name'),
        'best_ride_date':         best_ride.get('date'),
        'best_power_w':           best_pwr.get('w'),
        'best_power_date':        best_pwr.get('date'),
        'total_dist':             r1(sum(r['dist'] for r in rides)),
        'total_elev':             r0(sum(r['elev'] for r in rides)),
        'runs_30d':               last_run.get('runs', 0),
        'run_dist_30d':           last_run.get('dist', 0),
        'avg_run_pace':           last_run.get('avg_pace'),
        'strength_30d':           last_str.get('sessions', 0),
        'strength_per_month_avg': r1(_avg([m['sessions'] for m in mstr[-6:]])),
        'avg_hr_riding':          r1(_avg(hr30)),
        'avg_ride_dist':          r1(_avg([r['dist'] for r in r30])) if r30 else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD HTML UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def build_payload(data, hk_kpis, sk_kpis):
    """Assemble the full DATA object for the dashboard."""
    # Monthly steps: prefer recomputed from daily, fall back to cached monthly
    sd = data.get('steps_daily', [])
    monthly_steps = monthly_from_daily_steps(sd) if sd else data.get('monthly_steps', [])

    return {
        'hk':            hk_kpis,
        'sk':            sk_kpis,
        'rhr_daily':     data.get('rhr_daily', []),
        'monthly_vo2':   data.get('monthly_vo2', []),
        'body_temp':     data.get('body_temp', []),
        'spo2_daily':    data.get('spo2_daily', []),
        'monthly_steps': monthly_steps,
        'monthly_energy':data.get('monthly_energy', []),
        'weekly_cyc':    data.get('weekly_cyc', []),
        'monthly_cyc':   data.get('monthly_cyc', []),
        'power_trend':   data.get('power_trend', []),
        'monthly_run':   data.get('monthly_run', []),
        'monthly_str':   data.get('monthly_str', []),
        'rides':         data.get('rides', []),
    }

def update_dashboard_html(payload):
    """Replace the const DATA=…; line in index.html with fresh payload."""
    if not os.path.exists(INDEX_HTML):
        log(f'  ✗ {INDEX_HTML} not found. Run initial setup.')
        return False

    with open(INDEX_HTML, 'r', encoding='utf-8') as f:
        html = f.read()

    marker = 'const DATA='
    start  = html.find(marker)
    if start == -1:
        log('  ✗ Could not find "const DATA=" in index.html')
        return False

    end       = html.find('\n', start)
    if end == -1: end = len(html)
    json_str  = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    new_html  = html[:start] + f'{marker}{json_str};' + html[end:]

    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(new_html)

    size_kb = len(new_html.encode()) // 1024
    log(f'  Dashboard HTML updated ({size_kb} KB)')
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB PAGES DEPLOY
# ══════════════════════════════════════════════════════════════════════════════

def git_push():
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M')
    cmds = [
        ['git', '-C', DASHBOARD_DIR, 'add', 'index.html'],
        ['git', '-C', DASHBOARD_DIR, 'commit', '-m', f'Auto-update {ts}'],
        ['git', '-C', DASHBOARD_DIR, 'push', 'origin', 'main'],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            if any(x in out for x in ('nothing to commit', 'up to date')):
                log('  Git: nothing new to commit')
                return True
            log(f'  ✗ Git error ({" ".join(cmd[2:])}): {out}')
            log('    Tip: ensure git remote is set — run: git -C ~/health-dashboard remote -v')
            return False
    log(f'  ✓ Pushed to GitHub Pages')
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = set(sys.argv[1:])

    if '--diagnose' in args:
        log('══ Health Export Diagnostic ══')
        diagnose_icloud_export()
        return

    log('══ Health Dashboard Auto-Update ══')
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    # ── Step 1: Apple Health
    log('\n[1/6] Apple Health (iCloud export)...')
    fresh_hk = read_apple_health(days=400)
    if not fresh_hk:
        log('  ⚠ Apple Health unavailable — will use cached data')

    # ── Step 2: Strava
    log('\n[2/6] Strava...')
    activities = strava_fetch(days=400)
    fresh_sk   = process_strava(activities) if activities else None
    if not fresh_sk:
        log('  ⚠ Strava unavailable — will use cached data')

    # ── Step 3: Merge with historical cache
    log('\n[3/6] Merging with cache...')
    cache  = load_cache()
    merged = merge_into_cache(cache, fresh_hk, fresh_sk)
    save_cache(merged)
    log(f'  Cache: {len(merged.get("rhr_daily",[]))} RHR days, '
        f'{len(merged.get("rides",[]))} rides, '
        f'{len(merged.get("monthly_vo2",[]))} VO2 months')

    # ── Step 4: KPIs
    log('\n[4/6] Computing KPIs...')
    hk_kpis = compute_hk_kpis(merged)
    sk_kpis = compute_sk_kpis(merged)
    log(f'  RHR: {hk_kpis["rhr_current"]} bpm  |  '
        f'VO2: {hk_kpis["vo2_current"]}  |  '
        f'Rides 30d: {sk_kpis["rides_30d"]}')

    # ── Step 5: Update dashboard HTML
    log('\n[5/6] Updating dashboard HTML...')
    payload = build_payload(merged, hk_kpis, sk_kpis)
    if not update_dashboard_html(payload):
        log('ERROR: Dashboard update failed. Aborting.')
        sys.exit(1)

    # ── Step 6: Push to GitHub Pages
    log('\n[6/6] Pushing to GitHub Pages...')
    git_push()

    log(f'\n✓ Done — updated at {hk_kpis["last_updated"]}\n')


if __name__ == '__main__':
    main()
