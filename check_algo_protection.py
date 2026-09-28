# -*- coding: utf-8 -*-
"""交易所條件單（Algo 服務）保護稽核 —— 唯讀，回答兩個機器人自己答不出來的問題

**為什麼要有這支腳本。** 2026-09-25 查 SOL 第三次「方向不符」事故時發現：本專案長年記載的
「demo 的三個查詢 API 都看不到條件單、也撤不掉」**是查錯地方**。Binance 已把條件單
（STOP_MARKET／TAKE_PROFIT_MARKET…）搬到 **Algo 服務**，舊的 `fetch_open_orders`、
`fetch_order(id)`、原生 `fapiPrivateGetOpenOrders` 都看不到它們 —— 實測：

    fetch_open_orders(SOL)                        → 0 張
    fetch_open_orders(SOL, {'trigger': True})     → 2 張（SL 116.08／TP 119.6，closePosition）
    fetch_order(algoId)                           → -2013 Order does not exist
    fetch_order(algoId, {'trigger': True})        → 找到，status=open，觸發價正確
    positions_altcoin.json 的 sl_order_id/tp_order_id → **就是** algoId，本地存得完全正確

於是 `_sync_sl_tp._order_live()` 幾乎每顆倉位、開倉後第一個 15 分鐘掃描都會問到 -2013，
判定「單子死了」→ 重新補掛 → 交易所回 -4130（同向 closePosition 單已存在，因為它其實活著）
→ 記下 `sltp_4130_noted`，此後**再也不查**。那個「-4130 是常態」不是 demo 缺陷，是查詢走錯端點。

**這支腳本量兩件事：**

(1) **保護**：每個交易所持倉，是否真的有活的 SL（STOP_MARKET）與 TP（TAKE_PROFIT_MARKET）
    Algo 單，方向為持倉反向、closePosition=true，SL 觸發價離進場價約等於預期止損比
    （主流 1%／山寨 3.5%）。缺 SL 是 🚨（離開碼 1）；缺 TP、SL 價位不對（疑似上一筆
    倉位留下的單）、同向多張、沒有持倉卻還有 Algo 單，是 ⚠️。
(2) **歷史**：近 N 天 Algo 單的下場 —— FINISHED（並與帳本比對「成交量 vs 該平的量」
    抓超額成交）、REJECTED（被觸發卻被交易所拒絕：止損沒執行、倉位裸奔到下一次軟體掃描）、
    EXPIRED（倉位被別的路徑平掉後 closePosition 單自動失效，這是正常的自我清理）。

**已知的兩種壞下場（2026-09-22～24 實測，SOL）：**
  · **超額成交**：STOP_MARKET closePosition 被觸發後成交量遠大於持倉（43.68／87.36／112.96
    對 33.99／9.7／35.30），平倉後直接反向開出一個意外倉位。Algo 紀錄的 `quantity`
    在成交後才被寫成那個數字（未觸發的都是 0.0）。**數量不是交易所參數決定的**
    （LOT_SIZE／MARKET_LOT_SIZE／槓桿分級都對不上），而意外倉位的大小在 09-10、09-22、
    09-24 三次都是 77.66 顆 —— 原因**未解**，這支腳本只負責量、不解釋。
  · **被拒絕**：觸發時被 REJECTED（09-23 SL 116.61、09-24 TP 116.68，皆 SOL 多單），
    倉位留著、沒有交易所端保護，直到下一輪軟體掃描才平掉。

**偵測延遲**是這支腳本額外量的第三個數字：超額成交發生在交易所端，機器人要等下一輪
（≤15 分鐘）掃描才發現並接管。09-24 那次是 12.5 分鐘 —— 那段時間一個 2.2 倍尺寸的
意外倉位完全沒有 SL/TP。

**刻意不做的事：** 不改 `_order_live`（主程式裡負責確認條件單存活的函式，未收錄於此節錄）、
不改任何下單路徑。本專案的紀錄是「事故當下動下單路徑，正是把事故變大的方式」。先量：
若這支稽核抓到「持倉沒有活的 SL」的頻率夠高，才有資格要求把查詢改走
`params={'trigger': True}`（預先寫下的觸發條件：任一持倉出現 🚨，或 7 天內止損單
被拒絕 ≥2 次）。

用法（要在有 BINANCE_API_KEY／BINANCE_SECRET_KEY 的環境，也就是容器內）：
    docker compose exec -T coin-monitor python scripts/check_algo_protection.py [--days 3]
    python scripts/check_algo_protection.py --selftest
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from _console import force_utf8
    force_utf8()
except Exception:
    pass

TZ8 = timezone(timedelta(hours=8))
OVERFILL_RATIO = 1.05        # 成交量超過帳本平倉量 5% 以上才算超額（手續費／四捨五入的餘裕）
MATCH_BEFORE_S = 300         # 帳本平倉時間可以比觸發早 5 分鐘（時鐘差）
MATCH_AFTER_S = 25 * 60      # 也可以晚到 25 分鐘（下一輪掃描 ≤15 分鐘才寫帳）
SL_TOL = 0.5                 # SL 距離與預期止損比的相對容許（0.5 = ±50%）
LEDGERS = ['btc_trades.jsonl', 'eth_trades.jsonl', 'sol_trades.jsonl', 'altcoin_trades.jsonl']


def _f(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _ts(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(TZ8)
    except Exception:
        return None


def _live(a):
    return str(a.get('algoStatus')) in ('NEW', 'TRIGGERING', 'TRIGGERED')


def analyze_protection(positions, open_algos, majors, major_sl, alt_sl):
    """positions: [{'sym':'SOLUSDT','coin':'SOL','side':'long'|'short','amt':..,'entry':..}]
    回傳 [(level, text)]，level ∈ {'crit','warn','ok'}。"""
    out = []
    by_sym = {}
    for a in open_algos:
        by_sym.setdefault(a.get('symbol'), []).append(a)
    pos_syms = {p['sym'] for p in positions}
    for p in positions:
        want = 'SELL' if p['side'] == 'long' else 'BUY'
        mine = [a for a in by_sym.get(p['sym'], []) if _live(a) and a.get('closePosition')
                and a.get('side') == want]
        sls = [a for a in mine if a.get('orderType') == 'STOP_MARKET']
        tps = [a for a in mine if a.get('orderType') == 'TAKE_PROFIT_MARKET']
        exp = major_sl if p['coin'] in majors else alt_sl
        head = f"{p['coin']} {'多' if p['side'] == 'long' else '空'} {p['amt']:g} @ {p['entry']:g}"
        if not sls:
            out.append(('crit', f'{head}｜🚨 **沒有活的 SL Algo 單**（交易所端無止損，只剩每 15 分鐘的軟體兜底）'))
        else:
            if len(sls) > 1:
                out.append(('warn', f'{head}｜同向 SL Algo 單 {len(sls)} 張（closePosition 應只有一張）'))
            trig = _f(sls[0].get('triggerPrice'))
            d = abs(trig / p['entry'] - 1) if p['entry'] else 0
            # 方向：多單的 SL 必須低於進場、空單必須高於進場
            wrong_side = (trig >= p['entry']) if p['side'] == 'long' else (trig <= p['entry'])
            if wrong_side or abs(d - exp) > SL_TOL * exp:
                out.append(('warn', f'{head}｜⚠️ SL 觸發價 {trig:g}（距進場 {d:.2%}，預期約 {exp:.1%}'
                                    f'{"，且在進場價錯的一側" if wrong_side else ""}）—— 疑似上一筆倉位留下的單'))
            else:
                out.append(('ok', f'{head}｜✅ SL {trig:g}（距進場 {d:.2%}）'))
        if not tps:
            out.append(('warn', f'{head}｜⚠️ 沒有活的 TP Algo 單（只剩軟體止盈）'))
        elif len(tps) > 1:
            out.append(('warn', f'{head}｜同向 TP Algo 單 {len(tps)} 張'))
    for sym, arr in by_sym.items():
        live = [a for a in arr if _live(a)]
        if live and sym not in pos_syms:
            out.append(('warn', f'{sym}｜沒有持倉卻還有 {len(live)} 張活的 Algo 單（closePosition 單應在平倉時自動失效）'))
    return out


def _match_ledger(coin, trig_dt, rows):
    """在帳本裡找「這張 Algo 單觸發後被寫成平倉」的那一列：同幣、平倉時間落在
    [觸發-5 分, 觸發+25 分]，取最近的。找不到回 None（不猜）。"""
    best, best_gap = None, None
    for r in rows:
        if r.get('coin') != coin:
            continue
        ct = r.get('_close_dt')
        if ct is None:
            continue
        gap = (ct - trig_dt).total_seconds()
        if -MATCH_BEFORE_S <= gap <= MATCH_AFTER_S and (best is None or abs(gap) < abs(best_gap)):
            best, best_gap = r, gap
    return best, best_gap


def analyze_history(hist_by_sym, ledger_rows):
    """回傳 (lines, stats)。lines 是 [(level, text)]。"""
    lines, st = [], {'finished': 0, 'overfill': 0, 'rejected': 0, 'expired': 0, 'unmatched': 0,
                     'by_coin': {}}   # coin → [已配對成交數, 其中超額]
    for sym, arr in sorted(hist_by_sym.items()):
        coin = sym[:-4] if sym.endswith('USDT') else sym
        for a in sorted(arr, key=lambda x: _f(x.get('createTime'))):
            if not a.get('closePosition'):
                continue
            s = a.get('algoStatus')
            trig_ms = _f(a.get('triggerTime'))
            tdt = _ts(trig_ms) if trig_ms > 0 else None
            if s == 'EXPIRED':
                st['expired'] += 1
            elif s == 'REJECTED' and tdt is not None:
                st['rejected'] += 1
                lines.append(('warn', f'{coin}｜{a.get("orderType")} {a.get("side")} 觸發價 {a.get("triggerPrice")} '
                                      f'於 {tdt:%m-%d %H:%M:%S} 被觸發卻遭**拒絕**（倉位沒被平掉，裸奔到下一次軟體掃描）'))
            elif s == 'FINISHED' and tdt is not None:
                st['finished'] += 1
                q = _f(a.get('actualQty')) or _f(a.get('quantity'))
                # 帳本時間是 +08 的 naive 時間 —— 觸發時間也換成 +08 naive 再比
                row, gap = _match_ledger(coin, tdt.astimezone(TZ8).replace(tzinfo=None), ledger_rows)
                if row is None:
                    st['unmatched'] += 1
                    continue
                amt = _f(row.get('amount'))
                bc = st['by_coin'].setdefault(coin, [0, 0])
                bc[0] += 1
                if amt > 0 and q > amt * OVERFILL_RATIO:
                    st['overfill'] += 1
                    bc[1] += 1
                    lines.append(('warn',
                        f'{coin}｜**超額成交** {a.get("orderType")} {a.get("side")} 觸發 {tdt:%m-%d %H:%M:%S}：'
                        f'成交 {q:g}，帳本平倉量 {amt:g}（{q / amt:.2f} 倍，多出 {q - amt:g} 顆 → 反向意外倉位）；'
                        f'機器人在 {gap / 60:.1f} 分鐘後才寫帳／接管'))
    return lines, st


def load_ledger_rows(root):
    rows = []
    for fn in LEDGERS:
        p = Path(root) / fn
        if not p.exists():
            continue
        for line in io.open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            try:
                r['_close_dt'] = datetime.fromisoformat(str(r.get('close_time')))
            except Exception:
                r['_close_dt'] = None
            rows.append(r)
    return rows


def selftest():
    bad = []
    n = [0]

    def chk(ok, msg):
        n[0] += 1
        if not ok:
            bad.append(msg)

    def algo(sym, typ, side, trig, status='NEW', cp=True, **kw):
        d = {'symbol': sym, 'orderType': typ, 'side': side, 'triggerPrice': str(trig), 'algoStatus': status,
             'closePosition': cp, 'quantity': '0.0', 'actualQty': None, 'triggerTime': '0', 'createTime': '1'}
        d.update(kw)
        return d

    def pos(coin='SOL', side='long', amt=10, entry=100.0):
        return [{'sym': coin + 'USDT', 'coin': coin, 'side': side, 'amt': amt, 'entry': entry}]

    M = {'BTC', 'ETH', 'SOL'}

    def prot(p, a):
        return analyze_protection(p, a, M, 0.01, 0.035)

    # 1) 健康：SL 99（1%）＋TP 102 → 沒有 crit/warn
    o = prot(pos(), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 99.0), algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 102.0)])
    chk(not (any(l in ('crit', 'warn') for l, _ in o)), f'1) 健康持倉不該有警報 —— {o}')
    # 2) 沒有 SL → crit（這是離開碼 1 的唯一來源，突變：改成 warn 必須被抓到）
    o = prot(pos(), [algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 102.0)])
    chk(not (not any(l == 'crit' for l, _ in o)), f'2) 缺 SL 應為 crit —— {o}')
    # 3) 缺 TP → warn，不得是 crit
    o = prot(pos(), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 99.0)])
    chk(not (any(l == 'crit' for l, _ in o) or not any('沒有活的 TP' in t for _, t in o)), f'3) 缺 TP 應為 warn 而非 crit —— {o}')
    # 4) SL 價位離進場太遠（3.5% 而預期主流 1%）→ warn（疑似上一筆的單）
    o = prot(pos(), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 96.5), algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 102.0)])
    chk(not (not any('疑似上一筆倉位' in t for _, t in o)), f'4) SL 距離不對應報疑似殘留單 —— {o}')
    # 4b) 山寨幣同樣的 3.5% 是**對的**，不得誤報（防止把預期值寫死成主流）
    o = prot(pos('ARIA', 'long', 1000, 0.0361), [algo('ARIAUSDT', 'STOP_MARKET', 'SELL', 0.0361 * 0.965),
                                                  algo('ARIAUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 0.0361 * 1.15)])
    chk(not (any(l in ('crit', 'warn') for l, _ in o)), f'4b) 山寨 3.5% 止損是正確的，不該報 —— {o}')
    # 4c) 多單的 SL 在進場價上方 → 錯的一側，必須報
    o = prot(pos(), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 101.0), algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 102.0)])
    chk(not (not any('錯的一側' in t for _, t in o)), f'4c) SL 在進場價錯的一側應報 —— {o}')
    # 5) 同向兩張 SL → warn
    o = prot(pos(), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 99.0), algo('SOLUSDT', 'STOP_MARKET', 'SELL', 99.0),
                     algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'SELL', 102.0)])
    chk(not (not any('SL Algo 單 2 張' in t for _, t in o)), f'5) 同向多張 SL 應報 —— {o}')
    # 6) 沒有持倉卻有活的 Algo 單 → warn；而**已 FINISHED／EXPIRED 的不算殘留**
    o = prot([], [algo('ETHUSDT', 'STOP_MARKET', 'SELL', 2500)])
    chk(not (not any('沒有持倉卻還有' in t for _, t in o)), f'6) 無倉位的活單應報殘留 —— {o}')
    o = prot([], [algo('ETHUSDT', 'STOP_MARKET', 'SELL', 2500, status='EXPIRED'),
                  algo('ETHUSDT', 'STOP_MARKET', 'SELL', 2500, status='FINISHED')])
    chk(not (o), f'6b) EXPIRED／FINISHED 的單不是殘留，不該報 —— {o}')
    # 6c) 空單的保護方向是 BUY；拿 SELL 單當它的 SL 不算
    o = prot(pos(side='short'), [algo('SOLUSDT', 'STOP_MARKET', 'SELL', 101.0)])
    chk(not (not any(l == 'crit' for l, _ in o)), f'6c) 空單不能拿 SELL 單當 SL —— {o}')

    # ── 歷史 ──
    def led(coin, close_iso, amt):
        return {'coin': coin, '_close_dt': datetime.fromisoformat(close_iso), 'amount': amt}

    def ms(dt):
        return str(int(dt.timestamp() * 1000))

    t0 = datetime(2026, 9, 24, 21, 19, 53, tzinfo=TZ8)
    fin = lambda q, dt=t0: algo('SOLUSDT', 'STOP_MARKET', 'BUY', 114.42, status='FINISHED',        # noqa: E731
                                quantity=str(q), actualQty=str(q), triggerTime=ms(dt))
    rows = [led('SOL', '2026-09-24T21:32:23', 35.3045)]
    # 7) 超額成交：112.96 對 35.3045（3.2 倍）→ 抓到，且帶偵測延遲
    ls, st = analyze_history({'SOLUSDT': [fin(112.96)]}, rows)
    chk(st['overfill'] == 1 and any('超額成交' in t and '3.20 倍' in t for _, t in ls),
        f'7) 超額成交要抓到並帶倍數 —— {ls} {st}')
    chk(any('12.5 分鐘' in t for _, t in ls), f'7) 要帶偵測延遲 —— {ls}')
    chk(st['by_coin'].get('SOL') == [1, 1], f'7) 分幣統計應為 SOL [1,1] —— {st}')
    # 8) ★正常止損（成交量＝帳本量）不得誤報★（沒有這一項，7 可以靠「全部報」通過）
    ls, st = analyze_history({'SOLUSDT': [fin(35.30)]}, rows)
    chk(not (st['overfill'] != 0 or ls), f'8) 正常成交不得報超額 —— {ls} {st}')
    chk(st['by_coin'].get('SOL') == [1, 0], f'8) 正常成交分幣統計應為 SOL [1,0] —— {st}')
    # 9) 帳本列離太遠（>25 分鐘）→ 不配對、不猜（記入 unmatched）
    far = [led('SOL', '2026-09-24T23:59:00', 35.3045)]
    ls, st = analyze_history({'SOLUSDT': [fin(112.96)]}, far)
    chk(not (st['overfill'] != 0 or st['unmatched'] != 1), f'9) 配不到帳本就不猜 —— {ls} {st}')
    # 9b) 不同幣的帳本列不得配對
    ls, st = analyze_history({'SOLUSDT': [fin(112.96)]}, [led('ETH', '2026-09-24T21:32:23', 1.5)])
    chk(not (st['overfill'] != 0), f'9b) 別的幣不得配對 —— {ls} {st}')
    # 10) REJECTED：有觸發時間才算「被觸發卻被拒」；觸發時間為 0（從未觸發）不算
    rej = algo('SOLUSDT', 'STOP_MARKET', 'SELL', 116.61, status='REJECTED', triggerTime=ms(t0))
    ls, st = analyze_history({'SOLUSDT': [rej]}, rows)
    chk(not (st['rejected'] != 1 or not any('拒絕' in t for _, t in ls)), f'10) 被觸發後拒絕要報 —— {ls} {st}')
    never = algo('SOLUSDT', 'STOP_MARKET', 'SELL', 116.61, status='REJECTED', triggerTime='0')
    ls, st = analyze_history({'SOLUSDT': [never]}, rows)
    chk(not (st['rejected'] != 0), f'10b) 從未觸發的 REJECTED 不算 —— {ls} {st}')
    # 11) EXPIRED 只計數、不報（那是 closePosition 的正常自我清理）
    exp = algo('SOLUSDT', 'TAKE_PROFIT_MARKET', 'BUY', 111.02, status='EXPIRED')
    ls, st = analyze_history({'SOLUSDT': [exp]}, rows)
    chk(not (st['expired'] != 1 or ls), f'11) EXPIRED 只計數不報 —— {ls} {st}')

    if bad:
        print('自我測試失敗：')
        for b in bad:
            print('  ❌', b)
        return 1
    print('自我測試：%d/%d 情境通過（' % (n[0], n[0]) + '持倉保護：健康／缺 SL 為 crit／缺 TP 只是 warn／SL 距離不對／'
          '山寨 3.5% 不誤報／SL 在錯的一側／同向多張／無倉位殘留單且 FINISHED·EXPIRED 不算／空單方向；'
          '歷史：超額成交帶倍數與偵測延遲／正常成交不誤報／配不到帳本不猜／別的幣不配對／'
          '被觸發後拒絕才報／EXPIRED 只計數）')
    return 0


def main():
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    days = 3
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])

    import ccxt
    ex = ccxt.binance({'apiKey': os.environ['BINANCE_API_KEY'],
                       'secret': os.environ['BINANCE_SECRET_KEY'],
                       'options': {'defaultType': 'future'}})
    ex.enable_demo_trading(True)
    ex.load_markets()

    # 原版會 import 私有主程式讀目前的即時參數；公開節錄版固定用這組代表值
    # （多／山寨止損比例，僅供判斷「SL 觸發價落在合理範圍」用，非目前實際即時參數）。
    majors, major_sl, alt_sl = {'BTC', 'ETH', 'SOL'}, 0.01, 0.035

    positions = []
    for p in ex.fetch_positions():
        amt = _f(p.get('contracts'))
        if not amt:
            continue
        sym = (p.get('info') or {}).get('symbol') or p['symbol'].replace('/', '').split(':')[0]
        positions.append({'sym': sym, 'coin': p['symbol'].split('/')[0], 'side': (p.get('side') or '').lower(),
                          'amt': amt, 'entry': _f(p.get('entryPrice'))})

    r = ex.fapiPrivateGetOpenAlgoOrders({})
    open_algos = r if isinstance(r, list) else (r.get('orders') or [])

    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    syms = {p['sym'] for p in positions} | {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'} | {a.get('symbol') for a in open_algos}
    hist = {}
    for sym in sorted(s for s in syms if s):
        try:
            h = ex.fapiPrivateGetAllAlgoOrders({'symbol': sym, 'startTime': since, 'limit': 200})
            hist[sym] = h if isinstance(h, list) else (h.get('orders') or [])
        except Exception as e:
            print(f'  ⚠️ {sym} 查不到 Algo 歷史：{str(e)[:120]}')

    ledger = load_ledger_rows(Path(__file__).resolve().parent.parent)
    prot = analyze_protection(positions, open_algos, majors, major_sl, alt_sl)
    hl, st = analyze_history(hist, ledger)

    crit = [t for l, t in prot if l == 'crit']
    warn = [t for l, t in prot if l == 'warn'] + [t for l, t in hl if l == 'warn']
    ok = [t for l, t in prot if l == 'ok']
    print(f'Algo 條件單保護稽核（近 {days} 天）：'
          + ('🚨 有持倉缺少交易所端止損' if crit else ('⚠️ 有 ' + str(len(warn)) + ' 項要看' if warn else '全部健康')))
    print(f'  持倉 {len(positions)} 筆、活的 Algo 單 {len([a for a in open_algos if _live(a)])} 張；'
          f'近 {days} 天 closePosition 單：觸發成交 {st["finished"]}（其中超額 {st["overfill"]}、'
          f'配不到帳本 {st["unmatched"]}）、被觸發卻遭拒 {st["rejected"]}、自動失效 {st["expired"]}')
    if st['by_coin']:
        print('  已配對成交的超額率（分幣）：' + '；'.join(
            f'{c} {v[1]}/{v[0]}' for c, v in sorted(st['by_coin'].items())))
    for t in crit:
        print('  ' + t)
    for t in warn:
        print('  ' + t)
    for t in ok:
        print('  ' + t)
    sys.exit(1 if crit else 0)


if __name__ == '__main__':
    main()
