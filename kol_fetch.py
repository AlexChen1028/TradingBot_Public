# -*- coding: utf-8 -*-
"""
scripts/kol_fetch.py — 本機（住宅 IP）抓 KOL 新影片逐字稿，給 Claude 自動總結用。

雲端 VPS 抓不到字幕（機房 IP 被 YouTube 封），但本機住宅 IP 可以。
本腳本只做「確定性」的抓取，總結交給 Claude（cowork）。

模式：
  python kol_fetch.py            # 偵測新片→抓逐字稿→寫 notes/.kol_pending.json（不動 seen）
  python kol_fetch.py --mark vid1,vid2   # 成功套用後，把這些 vid 記為已處理（寫入 .kol_seen.json）

seen 檔 notes/.kol_seen.json 為「本機執行期狀態」，不進 git（各機獨立）。
"""
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import force_utf8    # noqa: E402
force_utf8()                       # Windows 主控台 cp950 會在 emoji 上炸掉

# Windows 主控台預設 cp950，印中文標題會 UnicodeEncodeError → 強制 utf-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    import feedparser
except Exception as e:
    print(json.dumps({'error': 'feedparser missing: %s' % e}))
    sys.exit(0)

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:
    YouTubeTranscriptApi = None

REPO_ROOT    = Path(__file__).resolve().parent.parent
SEEN_FILE    = REPO_ROOT / 'notes' / '.kol_seen.json'
PENDING_FILE = REPO_ROOT / 'notes' / '.kol_pending.json'

# channel_id 寫死（2026-06-19 解析）：避免每次 scrape youtube.com 首頁被限流 →
# 否則解析失敗會誤回 pending=0（假「沒新片」）。RSS 直接用 channel_id 穩定可靠。
KOL_CHANNELS = [
    {'handle': '@crypto_punks', 'name': '加密龐克', 'channel_id': 'UCeeeGbipVKpz23A8_c3I3uA'},
    {'handle': '@BTCfeiyang',   'name': 'BTC飛揚',  'channel_id': 'UCvuvTVzo8W9I6QOyZCXlubg'},
    {'handle': '@BTC-ouyang',   'name': 'BTC歐陽',  'channel_id': 'UCzZ49DculfIZv6W1X81pLlQ'},
]
WANT_LANGS      = ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en']
MAX_PER_CHANNEL = 6
MAX_TRANSCRIPT  = 14000   # 截斷過長逐字稿


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding='utf-8')).get('seen', []))
        except Exception:
            pass
    return set()


def save_seen(seen):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps({'seen': sorted(seen)}, indent=2), encoding='utf-8')


def resolve_channel_id(handle):
    try:
        r = requests.get('https://www.youtube.com/' + handle,
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        m = re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_transcript(vid):
    if YouTubeTranscriptApi is None:
        return None
    try:
        api = YouTubeTranscriptApi()
        tl  = api.list(vid)
        try:
            tr = tl.find_transcript(WANT_LANGS)
        except Exception:
            tr = next(iter(tl))
        data = tr.fetch()
        segs = [getattr(s, 'text', None) or (s.get('text', '') if isinstance(s, dict) else '') for s in data]
        return ' '.join(t for t in segs if t)[:MAX_TRANSCRIPT]
    except AttributeError:
        try:
            data = YouTubeTranscriptApi.get_transcript(vid, languages=WANT_LANGS)
            return ' '.join(d['text'] for d in data)[:MAX_TRANSCRIPT]
        except Exception:
            return None
    except Exception:
        return None


def whisper_transcript(vid):
    """後備：對關閉字幕的影片下載音訊→faster-whisper 轉文字（lazy import，慢）。

    回 (逐字稿或 None, 原因)。原因會一路傳到 `cmd_detect`，因為
    **「這支片沒有語音」與「轉錄壞了」需要完全不同的處理**：前者重試保證失敗。
    """
    try:
        from kol_whisper import transcribe_ex as _wt
    except Exception as e:
        print('  ⚠️ kol_whisper 不可用（%s），略過 Whisper 後備' % e)
        return None, 'unavailable'
    print('  🎙️ %s 無原生字幕 → Whisper 轉錄中（約數分鐘）…' % vid)
    txt, why = _wt(vid)
    if txt:
        print('  🎙️ %s Whisper 成功 %d 字' % (vid, len(txt)))
    elif why == 'no_speech':
        print('  🔇 %s 模型跑完但 0 段語音 —— 這支片沒有話（推廣短片／純音樂），'
              '不是轉錄失敗' % vid)
    else:
        print('  🎙️ %s Whisper 失敗（%s）' % (vid, why))
    return txt, why


def cmd_mark(ids_csv):
    ids = [i.strip() for i in ids_csv.split(',') if i.strip()]
    seen = load_seen()
    seen.update(ids)
    save_seen(seen)
    print('marked %d seen, total=%d' % (len(ids), len(seen)))


RETIRE_HOURS = 30   # 無字幕影片逾此時數仍抓不到 → 退休（標記 seen），停止每輪重撈
RETIRED_FILE = REPO_ROOT / 'notes' / '.kol_retired.jsonl'   # 退休紀錄（含標題），見 cmd_detect


def cmd_detect():
    seen = load_seen()
    pending = []
    retired = []
    now = datetime.now(timezone.utc)
    for ch in KOL_CHANNELS:
        cid = ch.get('channel_id') or resolve_channel_id(ch['handle'])
        if not cid:
            print('  [%s] 無 channel_id（解析失敗），略過' % ch['name'])
            continue
        feed = feedparser.parse('https://www.youtube.com/feeds/videos.xml?channel_id=' + cid)
        for e in feed.entries[:MAX_PER_CHANNEL]:
            vid = e.get('yt_videoid', '')
            if not vid or vid in seen:
                continue
            txt = get_transcript(vid)        # 快：原生字幕
            source = 'caption' if txt else ''
            why = 'caption' if txt else ''
            if not txt:
                # 字幕關閉（飛揚/歐陽）→ 音訊轉文字後備（慢，~5min/支）
                txt, why = whisper_transcript(vid)
                if txt:
                    source = 'whisper'
            if not txt:
                # ★no_speech 立即退休★ 模型已經跑完、只是這支片沒有語音 ——
                # 再試一百次也是同一個結果。2026-09-14 之前這種片會卡在清單裡
                # 30 小時、每輪重下載 684 KB 並重跑一次模型（約 60 次），
                # 而且每輪都在報告裡被寫成「轉錄失敗」。
                # 等待重試只對「可能會變」的失敗有意義。
                if why == 'no_speech':
                    retired.append({'vid': vid, 'kol': ch['name'],
                                    'title': e.get('title', '(無標題)'),
                                    'why': 'no_speech',
                                    'ts': now.isoformat()})
                    print('  ↳ 退休［%s］%s（%s）：無語音（模型跑完 0 段）' %
                          (ch['name'], e.get('title', '(無標題)')[:40], vid))
                    continue
                # 其餘失敗（下載被擋、模型例外）可能是暫時的 → 逾 RETIRE_HOURS 才退休
                pp = e.get('published_parsed')
                if pp:
                    age_h = (now - datetime(*pp[:6], tzinfo=timezone.utc)).total_seconds() / 3600
                    if age_h > RETIRE_HOURS:
                        retired.append({'vid': vid, 'kol': ch['name'],
                                        'title': e.get('title', '(無標題)'),
                                        'why': why or 'unknown',
                                        'ts': now.isoformat()})
                        print('  ↳ 退休［%s］%s（%s）：逾 %dh 仍抓不到字幕（why=%s）' %
                              (ch['name'], e.get('title', '(無標題)')[:40], vid,
                               RETIRE_HOURS, why or 'unknown'))
                        continue
            pending.append({
                'kol':   ch['name'],
                'vid':   vid,
                'title': e.get('title', '(無標題)'),
                'url':   e.get('link', 'https://www.youtube.com/watch?v=' + vid),
                'date':  (e.get('published', '') or '')[:10],
                # ★保留完整發布時間★ RSS 給的是完整 ISO 時間戳，而這裡原本只留
                # `[:10]` 的日期。KOL 條件登記簿的規則是「登記需要一個**時間**，
                # 不只日期」（`registered_at` 驅動 registered_at 盲區與小時線複核），
                # 而在此之前那個時間只能靠 KOL 剛好把它講出來（歐陽 9/15 說了
                # 「現在是上午 9 點 30 分」，飛揚同一天沒說）。也就是說登記精度
                # 取決於一件與資料無關的偶然 —— 而欄位一直都在，只是被截掉了。
                'published': e.get('published', '') or '',
                'transcript_ok': bool(txt),
                'source': source,
                'transcript': txt or '',
            })
    if retired:
        seen.update(r['vid'] for r in retired)
        save_seen(seen)
        # ★退休要留下可事後複查的紀錄★
        # `no_speech` 是**立即且永久**的退休（見上面那段註解），而在 2026-09-14
        # 之前它唯一的痕跡是某次 session 的主控台捲動 —— 連退休掉的是哪支片都
        # 只留一個 id。當天實際發生：飛揚的一支 22.5 秒會員招攬短片被退休，
        # 而要確認「這判斷對不對」必須手動去打 oEmbed 查標題。
        # 判斷本身是對的，但**一個無法被複查的永久決定，遲早會有一次是錯的而沒人知道**。
        # 所以標題隨紀錄一起落檔：日後只要掃一次這個檔，就能檢查是不是全都是短片。
        try:
            with RETIRED_FILE.open('a', encoding='utf-8') as f:
                for r in retired:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
        except Exception as ex:
            print('  ⚠️ 退休紀錄寫入失敗：%s: %s' % (type(ex).__name__, ex))
        n_ns = sum(1 for r in retired if r['why'] == 'no_speech')
        print('  退休 %d 支（無語音 %d、逾 %dh 抓不到字幕 %d），已標記 seen 並記入 %s'
              % (len(retired), n_ns, RETIRE_HOURS, len(retired) - n_ns,
                 RETIRED_FILE.name))
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
    ok = sum(1 for p in pending if p['transcript_ok'])
    print('pending=%d (transcript_ok=%d) -> %s' % (len(pending), ok, PENDING_FILE.name))
    for p in pending:
        print('  [%s] %s %s  字幕=%s' % (p['date'], p['kol'], p['title'][:40], '有' if p['transcript_ok'] else '無'))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--mark':
        cmd_mark(sys.argv[2])
    else:
        cmd_detect()
