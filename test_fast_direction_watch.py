# -*- coding: utf-8 -*-
"""Self-test for fast_direction_watch.py — no network, no real sleeping.

    python test_fast_direction_watch.py
"""
import fast_direction_watch as fw

SOL, BTC, ETH, ARIA = 'SOL/USDT:USDT', 'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ARIA/USDT:USDT'


def main():
    bad, n = [], [0]

    def chk(ok, msg):
        n[0] += 1
        if not ok:
            bad.append(msg)

    class Rig:
        """Test double: scripted exchange answers, records what happened."""
        def __init__(self, answers):
            self.answers = {k: list(v) for k, v in answers.items()}   # symbol -> answers in order
            self.queries, self.handled, self.sleeps = [], [], []

        def ex_position(self, ex, sym):
            self.queries.append(sym)
            a = self.answers.get(sym, [])
            return a.pop(0) if a else (0, 0.0)

        def handler(self, ex, sym, positions, ex_d, ex_amt, reason):
            self.handled.append((sym, ex_d, ex_amt, reason))

        def sleep(self, s):
            self.sleeps.append(s)

        def run(self, positions):
            return fw._fast_direction_watch(object(), positions, ex_position=self.ex_position,
                                             handler=self.handler, sleep=self.sleep)

    long_local = {SOL: {'direction': 1}}
    short_local = {SOL: {'direction': -1}}

    # 1) confirmed mismatch -> handed off, with the standard reason string
    r = Rig({SOL: [(1, 77.66), (1, 77.66)]})
    got = r.run(short_local)
    chk(got == [SOL] and r.handled == [(SOL, 1, 77.66, 'direction_reconcile')],
        f'1) confirmed mismatch should hand off -- {got} {r.handled}')
    chk(r.sleeps == [fw.FAST_WATCH_CONFIRM], f'1) should wait {fw.FAST_WATCH_CONFIRM}s before re-checking -- {r.sleeps}')
    # 2) direction matches -> no action, no extra wait (the common case, every tick)
    r = Rig({SOL: [(1, 42.68)]})
    chk(r.run(long_local) == [] and not r.handled and not r.sleeps, f'2) matching direction must not act -- {r.handled} {r.sleeps}')
    # 3) exchange is flat (0, 0.0): that's "already closed", not a mismatch -- leave it to the scan
    r = Rig({SOL: [(0, 0.0)]})
    chk(r.run(long_local) == [] and not r.handled, f'3) flat must not be treated as mismatch -- {r.handled}')
    # 4) query fails (None): don't guess, don't crash
    r = Rig({SOL: [None]})
    try:
        chk(r.run(long_local) == [] and not r.handled, f'4) failed query must not act -- {r.handled}')
    except Exception as e:
        chk(False, f'4) failed query must not raise -- {e!r}')
    # 5) transient: mismatch on first read, resolved by the second -> no action (why confirmation exists)
    r = Rig({SOL: [(-1, 10.0), (1, 42.68)]})
    chk(r.run(long_local) == [] and not r.handled, f'5) unconfirmed mismatch must not act -- {r.handled}')
    # 5b) second query fails -> also no action
    r = Rig({SOL: [(-1, 10.0), None]})
    chk(r.run(long_local) == [] and not r.handled, f'5b) failed re-check must not act -- {r.handled}')
    # 5c) both reads mismatch but the *quantity* differs (position still settling) -> no action
    r = Rig({SOL: [(-1, 10.0), (-1, 12.0)]})
    chk(r.run(long_local) == [] and not r.handled, f'5c) still-settling quantity must not act -- {r.handled}')
    # 6) no local record -> zero API calls (an idle account costs nothing per tick)
    r = Rig({})
    chk(r.run({}) == [] and r.queries == [], f'6) no local position must not query -- {r.queries}')
    # 7) altcoins are out of scope even if mismatched -- not what this mechanism covers
    r = Rig({ARIA: [(-1, 5.0), (-1, 5.0)]})
    chk(r.run({ARIA: {'direction': 1}}) == [] and r.queries == [], f'7) altcoins out of scope -- {r.queries}')
    # 8) multiple majors held at once: judged independently, only the true mismatch is handed off
    r = Rig({BTC: [(1, 0.05)], ETH: [(1, 1.5)], SOL: [(1, 77.66), (1, 77.66)]})
    got = r.run({BTC: {'direction': 1}, ETH: {'direction': 1}, SOL: {'direction': -1}})
    chk(got == [SOL] and len(r.handled) == 1, f'8) only the mismatched symbol should be handed off -- {got}')

    # -- _sleep_and_watch --
    class Clock:
        def __init__(self):
            self.t = 1000.0
            self.sleeps, self.watches = [], 0

        def now(self):
            return self.t

        def sleep(self, s):
            self.sleeps.append(s)
            self.t += s

        def watch(self, ex, pos):
            self.watches += 1

    def run_sleep(total, watch=None):
        c = Clock()
        fw._sleep_and_watch(object(), {}, total, watch=watch or c.watch, sleep=c.sleep, clock=c.now)
        return c

    # 9) total elapsed time is unchanged; watches happen between chunks, not after the last one
    c = run_sleep(300)
    chk(abs(sum(c.sleeps) - 300) < 1e-9, f'9) total sleep should be 300 -- {sum(c.sleeps)}')
    chk(c.watches == 4, f'9) 300s should watch 4 times (after chunks 1-4, not the last) -- {c.watches}')
    # 9b) production constant: a 15-minute scan interval -> 14 watches
    c = run_sleep(15 * 60)
    chk(abs(sum(c.sleeps) - 15 * 60) < 1e-9 and c.watches == 14,
        f'9b) 900s should watch 14 times with total time unchanged -- {c.watches} {sum(c.sleeps)}')
    # 10) shorter than one chunk -> sleep through, no watch
    c = run_sleep(30)
    chk(c.sleeps == [30] and c.watches == 0, f'10) under one chunk should not watch -- {c.sleeps} {c.watches}')
    # 11) an exception inside watch() must not affect sleep timing or escape
    boom = {'n': 0}

    def bad_watch(ex, pos):
        boom['n'] += 1
        raise RuntimeError('boom')
    try:
        c = run_sleep(300, watch=bad_watch)
        chk(abs(sum(c.sleeps) - 300) < 1e-9 and boom['n'] == 4,
            f'11) should still run to completion after an exception -- {boom} {sum(c.sleeps)}')
    except Exception as e:
        chk(False, f'11) an exception from watch() must not escape -- {e!r}')

    if bad:
        print('FAILED:')
        for b in bad:
            print('  -', b)
        return 1
    print(f'{n[0]}/{n[0]} scenarios passed')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
