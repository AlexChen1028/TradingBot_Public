# -*- coding: utf-8 -*-
"""stale_gate_report.py —— 三態稽核回報：「全部健康」與「不知道」不能共用綠燈

節錄自完整稽核腳本 `check_stale_gates.py` 中最值得單獨拿出來看的一段。原始
版本還會連上交易所、讀取即時風控常數、對 `monitor_coins.py` 本身的原始碼跑
AST 掃描去驗證一條不變量——那些部分深度依賴這個私有節錄沒有收錄的主程式，
拿掉之後就不是真正在跑的程式碼了，所以沒有收錄。這裡收錄的 `render()` 是
可以完全獨立驗證、也真的在生產環境用同一份程式碼跑的那一段。

## 這支稽核在查什麼

這個交易機器人有一批「陳舊閘門稽核」，定期檢查每一個會影響下單的風控常數
（支撐/壓力帶、方向性偏向開關、幣種黑名單…）是否還有現行依據支撐。`render()`
把稽核結果（issues／notes）轉成人類可讀的報告，決定離開碼——聽起來像是最
不值得討論的一段，但這裡曾經真的藏過一個 bug。

## 藏過的 bug

早期版本對每一筆檢查項目都硬印一個綠色勾勾 ✅，不分青紅皂白。稽核機制有一部
分依賴另一個檔案（責任週期統計）——那個檔案只存在於正式環境的容器裡，開發機
上永遠讀不到。結果就是：開發機每次執行都印出一片綠燈，看起來「全部健康」，
但其實是「這部分完全沒有被檢查到」。這兩件事被同一個符號、同一個離開碼表達，
於是一整條稽核邏輯安靜地不存在了一段時間，而畫面上完全沒有任何異狀。

## 修法

把結果分成明確的三態，而不是布林值：

1. **有問題**（issues）—— 明確地壞了，離開碼 1，一定要印出來。
2. **查不到**（blocked，用 `⛔` 標記）—— 不是「查了沒問題」，是「根本沒查到」，
   離開碼 2，同樣一定要印出來，`--quiet` 對它沒有作用。
3. **全部健康** —— 真的檢查過、真的沒問題，離開碼 0，`--quiet` 之下才可以
   完全不輸出。

`--quiet` 只能讓「真的健康」閉嘴；「查不到」與「有問題」都必須出聲，否則
排程永遠學不到系統其實在裸奔。

用法：
    python stale_gate_report.py
"""
try:
    from _console import force_utf8
    force_utf8()   # Windows 主控台預設 cp950，印下面這些中文字會亂碼／出錯——見 _console.py
except Exception:
    pass

BLOCKED_MARK = '⛔'


def render(issues, notes, quiet=False, blocked_mark=BLOCKED_MARK):
    """把稽核結果算成 (要印的行, 離開碼)。

    抽成純函式的理由：原本的 bug 在**渲染層**（寫死 ✅），不在稽核的判定本身。
    判定只能有一份，而且要能被測試——這也是為什麼這個檔案值得單獨拿出來看。

    三態：issues 優先（1）→ 有 blocked 的項目（2，無法判定）→ 全綠（0）。
    """
    blocked = [n for n in notes if str(n).startswith(blocked_mark)]
    plain = [n for n in notes if not str(n).startswith(blocked_mark)]
    lines = []
    if issues:
        lines.append('=' * 66)
        lines.append('  稽核：發現 %d 個需要處理的項目' % len(issues))
        lines.append('=' * 66)
        lines += ['  WARN %s' % i for i in issues]
        lines.append('')
    elif blocked:
        # 不得說「全部健康」：有東西沒查到，健康與否就是未知的。
        lines.append('稽核：無陳舊項目，但有 %d 項**無法判定**（沒查到，不是沒問題）。'
                     % len(blocked))
        lines.append('')
    elif not quiet:
        lines.append('稽核：全部健康，無需處理。')
        lines.append('')
    # blocked 不受 quiet 影響：一個安靜的「沒查到」正是這支稽核在防的那個假綠燈。
    for b in blocked:
        lines.append('  %s' % b)
    if not quiet:
        lines.append('-- 明細 --')
        for n in plain:
            lines.append('  OK %s' % n)
    return lines, (1 if issues else (2 if blocked else 0))


def selftest():
    B = BLOCKED_MARK
    bad = []
    # 1) 全綠：要說得出「全部健康」，exit 0
    l1, c1 = render([], ['範例：某閘門責任週期 48%'])
    if c1 != 0 or not any('全部健康' in x for x in l1):
        bad.append('1) 全綠要 exit 0 並說得出全部健康 -- code=%s' % c1)
    # 2) 有 issues：exit 1
    l2, c2 = render(['某閘門過寬'], [])
    if c2 != 1 or not any('某閘門過寬' in x for x in l2):
        bad.append('2) 有 issues 要 exit 1 並印出來 -- code=%s' % c2)
    # 3) 只有「沒查到」時，不得 exit 0、不得說全部健康（這就是曾經藏過的那個 bug）
    l3, c3 = render([], [B + ' 責任週期統計：讀不到資料檔'])
    if c3 != 2:
        bad.append('3) 只有沒查到時要 exit 2（無法判定），得到 %s' % c3)
    elif any('全部健康' in x for x in l3):
        bad.append('3) 有東西沒查到時不得宣稱全部健康 -- %s' % l3)
    elif not any('無法判定' in x for x in l3):
        bad.append('3) 要明說無法判定 -- %s' % l3)
    # 4) 沒查到的那一筆不得被蓋上綠燈
    if any(x.strip().startswith('OK') and '責任週期' in x for x in l3):
        bad.append('4) 沒查到的項目被蓋上綠燈 -- %s' % l3)
    if not any(x.strip().startswith(B) for x in l3):
        bad.append('4) 沒查到的項目要以 %s 呈現 -- %s' % (B, l3))
    # 5) issues 與 blocked 同時存在：issues 優先（1），但 blocked 仍要看得見
    l5, c5 = render(['某閘門過寬'], [B + ' 讀不到資料檔'])
    if c5 != 1:
        bad.append('5) issues 優先於 blocked，應 exit 1，得到 %s' % c5)
    elif not any(x.strip().startswith(B) for x in l5):
        bad.append('5) blocked 不得被 issues 蓋掉 -- %s' % l5)
    # 6) 普通 note 仍要有綠燈（否則就是把綠燈整個拿掉，換一個反向的謊）
    if not any(x.strip().startswith('OK') for x in l1):
        bad.append('6) 普通 note 仍要有綠燈 -- %s' % l1)
    # 7) 三種表頭必須互不相同
    heads = {l1[0], l2[1], l3[0]}
    if len(heads) != 3:
        bad.append('7) 全綠/有問題/沒查到的表頭有重複 -- %s' % sorted(heads))
    # 8) --quiet 之下，沒查到仍要出聲，全綠才可以閉嘴
    l8, c8 = render([], [B + ' 讀不到資料檔'], quiet=True)
    if c8 != 2 or not any(x.strip().startswith(B) for x in l8):
        bad.append('8) quiet 不得讓「沒查到」消失 -- code=%s %s' % (c8, l8))
    l8b, c8b = render([], ['一切正常'], quiet=True)
    if c8b != 0 or l8b:
        bad.append('8b) quiet 之下全綠應完全不輸出 -- %s' % l8b)

    for b in bad:
        print('  FAIL %s' % b)
    print('自我測試：%d/8 情境通過（全綠 exit 0／有問題 exit 1／只有沒查到要 exit 2／'
          '沒查到不得蓋綠燈／issues 優先但 blocked 仍可見／普通 note 仍有綠燈／'
          '三種表頭互不相同／quiet 不得吃掉沒查到）' % (8 - len(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    import sys
    sys.exit(selftest())
