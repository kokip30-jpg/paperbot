#!/usr/bin/env python3
"""Papírový bot: kupuje po nákupech insiderů z vedení firem, drží 90 dní.

Obchoduje výhradně na papírovém účtu Alpaca (paper-api.alpaca.markets).
Stav (otevřené pozice, historie) se ukládá do složky state/.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, 'state')
CFG = json.load(open(os.path.join(ROOT, 'config.json'), encoding='utf-8'))
RULE = CFG['rule']
KEY, SECRET = os.environ.get('ALPACA_KEY', '').strip(), os.environ.get('ALPACA_SECRET', '').strip()
TOPIC = os.environ.get('NTFY_TOPIC', '').strip()
BASE = 'https://paper-api.alpaca.markets'   # nikdy neměnit na ostrý účet
NOW = datetime.now(timezone.utc)
EXEC_RE = re.compile(r'\b(CEO|CFO|COO|CTO|Chief|President|Chair|Chairman|Founder|Executive Vice)\b', re.I)
LOG = []


def log(*a):
    line = ' '.join(str(x) for x in a)
    LOG.append(line)
    print(time.strftime('%H:%M:%S'), line, flush=True)


def http(url, data=None, method='GET', headers=None, tries=3):
    h = {'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET, 'Accept': 'application/json'}
    if data is not None:
        h['Content-Type'] = 'application/json'
    if headers:
        h.update(headers)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode('utf-8', 'replace')
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            log('HTTP', e.code, url.split('?')[0], detail)
            return None
        except Exception as e:
            log('síť', repr(e)[:120])
            time.sleep(2 * (i + 1))
    return None


def load(name, default):
    try:
        with open(os.path.join(STATE, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def ntfy(title, message, priority=3):
    if not TOPIC:
        return
    body = json.dumps({'topic': TOPIC, 'title': title[:200], 'message': message[:1500],
                       'priority': priority, 'tags': ['robot']}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request('https://ntfy.sh/', data=body,
                                                      headers={'Content-Type': 'application/json'}), timeout=15)
    except Exception as e:
        log('ntfy selhalo', repr(e)[:80])


def is_exec(r):
    return bool(r.get('of')) or EXEC_RE.search(r.get('r') or '') is not None


def candidates():
    """Nákupy insiderů z vedení firem za posledních pár dní."""
    try:
        with urllib.request.urlopen(CFG['source'], timeout=40) as resp:
            rows = json.loads(resp.read())
    except Exception as e:
        log('zdroj dat selhal', repr(e)[:120])
        return []
    cutoff = (NOW.date() - timedelta(days=RULE['max_filing_age_days'])).isoformat()
    deals = {}
    for r in rows:
        if r.get('k') != 'P' or not r.get('t') or (r.get('f') or '') < cutoff:
            continue
        if not is_exec(r) or r['t'] in RULE['skip_tickers'] or r['t'].endswith('-USD'):
            continue
        if (r.get('p') or 0) < RULE['min_price']:
            continue
        key = (r.get('a'), r.get('o'))
        d = deals.setdefault(key, {'t': r['t'], 'o': r.get('o'), 'r': r.get('r'), 'f': r.get('f'), 'value': 0.0})
        d['value'] += (r.get('s') or 0) * (r.get('p') or 0)
    out = [d for d in deals.values() if d['value'] >= RULE['min_value']]
    out.sort(key=lambda d: (d['f'], d['value']), reverse=True)
    return out


def main():
    if not (KEY and SECRET):
        log('chybí klíče Alpaca')
        return
    clock = http(f'{BASE}/v2/clock')
    if not clock:
        log('účet Alpaca neodpovídá')
        return
    if not clock.get('is_open'):
        log('burza je zavřená, jen kontrola stavu')
    account = http(f'{BASE}/v2/account') or {}
    equity = float(account.get('equity') or 0)
    positions = http(f'{BASE}/v2/positions') or []
    held = {p['symbol']: p for p in positions}
    state = load('positions.json', {})
    history = load('history.json', [])
    log(f'účet {account.get("status")} · hodnota {equity:,.0f} $ · pozic {len(held)}'.replace(',', ' '))

    # 1) prodej po uplynutí doby držení
    if clock.get('is_open'):
        for sym, info in list(state.items()):
            if sym not in held:
                state.pop(sym, None)
                continue
            opened = datetime.fromisoformat(info['opened'])
            age = (NOW - opened).days
            if age < RULE['hold_days']:
                continue
            qty = held[sym]['qty']
            order = http(f'{BASE}/v2/orders', json.dumps({'symbol': sym, 'qty': qty, 'side': 'sell',
                                                          'type': 'market', 'time_in_force': 'day'}).encode(), 'POST')
            if order:
                pl = float(held[sym].get('unrealized_plpc') or 0) * 100
                log(f'prodej {sym} po {age} dnech, výsledek {pl:+.1f} %')
                history.append({'t': sym, 'opened': info['opened'], 'closed': NOW.isoformat(),
                                'entry': info.get('entry'), 'exit': float(held[sym].get('current_price') or 0),
                                'pl_pct': round(pl, 2), 'reason': f'{RULE["hold_days"]} dní'})
                ntfy(f'Bot prodal {sym}', f'Po {age} dnech, výsledek {pl:+.1f} %.')
                state.pop(sym, None)

    # 2) nové nákupy
    bought = 0
    if clock.get('is_open') and equity > 0:
        size = equity * RULE['position_pct'] / 100
        for d in candidates():
            if bought >= RULE['max_new_per_day'] or len(state) >= RULE['max_positions']:
                break
            sym = d['t']
            if sym in state or sym in held:
                continue
            if any(h.get('t') == sym and h.get('closed', '') > (NOW - timedelta(days=30)).isoformat() for h in history):
                continue  # po prodeji měsíc pauza
            order = http(f'{BASE}/v2/orders', json.dumps({'symbol': sym, 'notional': round(size, 2), 'side': 'buy',
                                                          'type': 'market', 'time_in_force': 'day'}).encode(), 'POST')
            if not order:
                continue
            bought += 1
            state[sym] = {'opened': NOW.isoformat(), 'reason': f"{d.get('o')} ({d.get('r')}) koupil za {d['value']:,.0f} $".replace(',', ' '),
                          'filed': d['f'], 'size': round(size, 2)}
            log(f'nákup {sym} za {size:,.0f} $ · {state[sym]["reason"]}'.replace(',', ' '))
            ntfy(f'Bot koupil {sym}', f"{state[sym]['reason']}\nPozice {size:,.0f} $ (papírový účet).".replace(',', ' '))

    # 3) uložit stav a přehled pro web
    save('positions.json', state)
    save('history.json', history[-500:])
    closed = [h for h in history if h.get('pl_pct') is not None]
    save('summary.json', {
        'updated': NOW.isoformat(),
        'equity': equity,
        'cash': float(account.get('cash') or 0),
        'start_equity': load('summary.json', {}).get('start_equity') or equity,
        'open': [{'t': p['symbol'], 'qty': float(p['qty']), 'entry': float(p['avg_entry_price']),
                  'price': float(p.get('current_price') or 0), 'pl_pct': round(float(p.get('unrealized_plpc') or 0) * 100, 2),
                  'opened': state.get(p['symbol'], {}).get('opened'), 'reason': state.get(p['symbol'], {}).get('reason')}
                 for p in positions],
        'closed': closed[-50:],
        'stats': {'trades': len(closed),
                  'win_rate': round(sum(1 for h in closed if h['pl_pct'] > 0) / len(closed) * 100) if closed else None,
                  'avg_pl': round(sum(h['pl_pct'] for h in closed) / len(closed), 2) if closed else None},
        'rule': RULE, 'log': LOG[-40:],
    })
    log('hotovo')


if __name__ == '__main__':
    main()
