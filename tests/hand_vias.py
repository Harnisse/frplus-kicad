"""人手配線のビアが、Freerouting 独自の規則 (同じネットのパッド・ビアにもクリアランスを要求) に
どれだけ反しているかを数える (KiCad 付属 python で実行)。

  python.exe hand_vias.py <board.kicad_pcb> [クリアランス mm]
"""
import collections
import math
import sys

import pcbnew

b = pcbnew.LoadBoard(sys.argv[1])
CLR = float(sys.argv[2]) if len(sys.argv) > 2 else pcbnew.ToMM(b.GetDesignSettings().m_MinClearance)
mm = pcbnew.ToMM


def poly(pad, layer):
    ps = pcbnew.SHAPE_POLY_SET()
    pad.TransformShapeToPolygon(ps, layer, 0, 5000, pcbnew.ERROR_INSIDE)
    o = ps.Outline(0)
    return [(mm(o.CPoint(i).x), mm(o.CPoint(i).y)) for i in range(o.PointCount())]


def dist(pts, x, y):
    inside, d = False, 1e9
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        dx, dy = x2 - x1, y2 - y1
        t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / ((dx * dx + dy * dy) or 1)))
        d = min(d, math.hypot(x - x1 - t * dx, y - y1 - t * dy))
    return -d if inside else d


pads = collections.defaultdict(list)
for fp in b.GetFootprints():
    for p in fp.Pads():
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if p.IsOnLayer(layer):
                pads[p.GetNetname()].append(('%s-%s' % (fp.GetReference(), p.GetNumber()), poly(p, layer)))
vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
c = collections.Counter()
rows = []
for v in vias:
    x, y, r = mm(v.GetPosition().x), mm(v.GetPosition().y), mm(v.GetWidth(pcbnew.F_Cu)) / 2
    gap, name = min(((dist(pts, x, y) - r, name) for name, pts in pads[v.GetNetname()]), default=(99, '-'))
    vg = min((math.hypot(x - mm(w.GetPosition().x), y - mm(w.GetPosition().y)) - r - mm(w.GetWidth(pcbnew.F_Cu)) / 2
              for w in vias if w is not v and w.GetNetname() == v.GetNetname()), default=99)
    if gap < -r:
        k = 'パッドの中 (ビアインパッド)'
    elif gap < 0:
        k = '同ネットのパッドに重なる'
    elif gap < CLR - 0.001:
        k = '同ネットのパッドから %.2fmm 未満' % CLR
    elif vg < CLR - 0.001:
        k = '同ネットのビアから %.2fmm 未満' % CLR
    else:
        k = 'Freerouting でも置ける'
    c[k] += 1
    if k != 'Freerouting でも置ける':
        rows.append((v.GetNetname(), name, round(gap, 3), round(vg, 3)))
print('ビア %d 個 (判定クリアランス %.3f mm)' % (len(vias), CLR))
for k, n in c.most_common():
    print('  %-34s %d' % (k, n))
nets = collections.Counter(r[0] for r in rows)
print('置けないビアのネット別:', dict(nets.most_common(12)))
if len(sys.argv) > 3:       # 3 番目の引数にネット名を並べると、そのネットの置けないビアを列挙する
    for r in rows:
        if r[0] in sys.argv[3].split(','):
            print('  %s: 最寄りの同ネットパッド %s まで %.3f mm、同ネットのビアまで %.3f mm' % r)
