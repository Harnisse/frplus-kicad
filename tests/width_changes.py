"""同じネットの線がつながる点で線幅が変わっている箇所を列挙する (KiCad 付属 python で実行)。

  python.exe width_changes.py <board.kicad_pcb>
"""
import collections
import math
import sys

import pcbnew

b = pcbnew.LoadBoard(sys.argv[1])
mm = pcbnew.ToMM
ends = collections.defaultdict(list)
for t in b.GetTracks():
    if t.Type() == pcbnew.PCB_VIA_T:
        continue
    for p in (t.GetStart(), t.GetEnd()):
        ends[(t.GetNetname(), t.GetLayerName(), p.x, p.y)].append(t)
pads = collections.defaultdict(list)
for fp in b.GetFootprints():
    for p in fp.Pads():
        bb = p.GetBoundingBox()
        pads[p.GetNetname()].append((mm(p.GetPosition().x), mm(p.GetPosition().y), mm(min(p.GetSize().x, p.GetSize().y)),
                                     '%s-%s' % (fp.GetReference(), p.GetNumber()), mm(max(bb.GetWidth(), bb.GetHeight())) / 2))
rows = []
for (net, layer, x, y), ts in ends.items():
    ws = sorted({round(mm(t.GetWidth()), 3) for t in ts})
    if len(ws) < 2:
        continue
    x, y = mm(x), mm(y)
    d, pw, name, reach = min(((math.hypot(x - px, y - py), pw, name, reach) for px, py, pw, name, reach in pads[net]),
                             default=(999, 0, '-', 0))
    narrow = min(ts, key=lambda t: t.GetWidth())
    rows.append((d - reach, net, layer, ws, name, pw, mm(narrow.GetLength()), narrow.IsLocked()))
rows.sort()
print('線幅が変わる点: %d か所' % len(rows))
print('%-22s %-6s %-14s %-9s %-7s %-9s %s' % ('ネット', '層', '幅 mm', '近いパッド', 'パッド幅', 'パッド縁から', '細い側の長さ'))
for d, net, layer, ws, name, pw, ln, locked in rows:
    print('%-22s %-6s %-14s %-9s %-7.2f %-9.2f %.2f%s' % (net[:22], layer, '/'.join('%.2f' % w for w in ws), name, pw, max(d, 0), ln,
                                                         ' (ロック済み)' if locked else ''))
