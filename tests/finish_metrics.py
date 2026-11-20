"""配線の仕上がりを数える (KiCad 付属 python で実行): 折れ角の内訳、短い線分、ビア直近の折り返し。

  python.exe finish_metrics.py <board.kicad_pcb> [...]
"""
import collections
import math
import sys

import pcbnew

mm = pcbnew.ToMM


def measure(path):
    b = pcbnew.LoadBoard(path)
    ends = collections.defaultdict(list)
    segs, vias = [], []
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            vias.append(t)
        elif t.Type() == pcbnew.PCB_TRACE_T and not t.IsLocked():
            segs.append(t)
            for p, q in ((t.GetStart(), t.GetEnd()), (t.GetEnd(), t.GetStart())):
                ends[(t.GetNetname(), t.GetLayer(), p.x, p.y)].append((q.x - p.x, q.y - p.y, t))
    c = collections.Counter()
    for key, lst in ends.items():
        if len(lst) != 2:
            if len(lst) > 2:
                c['分岐点'] += 1
            continue
        (ax, ay, ta), (bx, by, tb) = lst
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la == 0 or lb == 0:
            continue
        ang = math.degrees(math.acos(max(-1, min(1, (ax * bx + ay * by) / (la * lb)))))   # 180 = 直進
        if ang > 170:
            c['直進のまま分割'] += 1
        elif ang > 125:
            c['45度の折れ'] += 1
        elif ang > 80:
            c['90度の折れ'] += 1
        else:
            c['鋭角の折れ'] += 1
    c['線分'] = len(segs)
    c['0.3mm 未満の線分'] = sum(1 for t in segs if mm(t.GetLength()) < 0.3)
    c['ビア'] = len(vias)
    c['配線長 mm'] = round(sum(mm(t.GetLength()) for t in segs))
    off = sum(1 for t in segs if (lambda dx, dy: dx and dy and abs(abs(dx) - abs(dy)) > 2000)(t.GetEnd().x - t.GetStart().x, t.GetEnd().y - t.GetStart().y))
    c['45度刻みでない線分'] = off
    return c


keys = ['配線長 mm', 'ビア', '線分', '45度の折れ', '90度の折れ', '鋭角の折れ', '直進のまま分割', '0.3mm 未満の線分', '45度刻みでない線分', '分岐点']
rows = [(p, measure(p)) for p in sys.argv[1:]]
print('%-18s' % '' + ''.join('%14s' % ('(%d)' % (i + 1)) for i in range(len(rows))))
for k in keys:
    print('%-18s' % k + ''.join('%14d' % r[k] for _, r in rows))
for i, (p, _) in enumerate(rows):
    print('(%d) %s' % (i + 1, p))
