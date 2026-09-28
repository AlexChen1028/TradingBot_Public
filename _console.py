# -*- coding: utf-8 -*-
"""主控台編碼修正：讓 CLI 在 Windows 上也能印出 emoji。

Windows 主控台預設是 cp950（繁中）而不是 UTF-8，`print('✅')` 會直接丟
`UnicodeEncodeError` 把整個腳本炸掉。這不是「輸出比較醜」而已，是**真的會壞事**：

2026-09-07 的例行稽核就踩到了 —— `check_stale_gates.py` 已經印完「全部健康」
的結論，接著在印明細的 ✅ 上當掉。結論看得到、明細看不到，而且行程以
traceback 收場。對 `--quiet` 的 cron 用法來說更糟：沒有陳舊項目（該回 0）
的正常情況會變成非零離開碼，等於製造假警報。

VPS 容器內是 UTF-8，所以這個問題只在本機出現 —— 也就是只在「人在看」的
那一次出現，正好是最不該漏字的時候。
"""
import sys


def force_utf8():
    """把 stdout/stderr 轉成 UTF-8；轉不動就退化成不會中斷的替代字元。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass        # 被重導向到不支援 reconfigure 的物件時，安靜略過
