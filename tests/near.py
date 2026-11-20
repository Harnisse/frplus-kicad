"""DRC の未接続項目の周辺にある銅を列挙する (KiCad 付属 python で実行)。

  python.exe near.py <board.kicad_pcb> <drc.json> [半径 mm]
"""
import json
import math
import sys

import pcbnew

b = pcbnew.LoadBoard(sys.argv[1])
d = json.load(open(sys.argv[2], encoding='utf-8'))
R = float(sys.argv[3]) if len(sys.argv) > 3 else 0.6
mm = pcbnew.ToMM
items = {}
for t in b.GetTracks():
    items[t.m_Uuid.AsString()] = t
for fp in b.GetFootprints():
    for p in fp.Pads():
        items[p.m_Uuid.AsString()] = p


def desc(it):
    if isinstance(it, pcbnew.PCB_VIA):
        return 'via  [%s] (%.3f,%.3f) d=%.2f' % (it.GetNetname(), mm(it.GetPosition().x), mm(it.GetPosition().y), mm(it.GetWidth(pcbnew.F_Cu)))
    if isinstance(it, pcbnew.PAD):
        bb = it.GetBoundingBox()
        return 'pad  [%s] %s-%s (%.3f,%.3f) %.2fx%.2f %s' % (it.GetNetname(), it.GetParentFootprint().GetReference(), it.GetNumber(),
                                                          mm(it.GetPosition().x), mm(it.GetPosition().y), mm(bb.GetWidth()), mm(bb.GetHeight()),
                                                          'F' if it.IsOnLayer(pcbnew.F_Cu) else 'B')
    return 'trk  [%s] %s (%.3f,%.3f)-(%.3f,%.3f) w=%.3f' % (it.GetNetname(), it.GetLayerName(), mm(it.GetStart().x), mm(it.GetStart().y),
                                                          mm(it.GetEnd().x), mm(it.GetEnd().y), mm(it.GetWidth()))


for u in d['unconnected_items']:
    ends = [items.get(i['uuid']) for i in u['items']]
    if any(e is None for e in ends):
        continue                        # ゾーンどうし
    print('---')
    for e in ends:
        print('  ', desc(e))
    net = ends[0].GetNetname()
    pts = []
    for e in ends:
        pts += [e.GetPosition()] if not isinstance(e, pcbnew.PCB_TRACK) or isinstance(e, pcbnew.PCB_VIA) else [e.GetStart(), e.GetEnd()]
    seen = set()
    for it in items.values():
        if it in ends or it.m_Uuid.AsString() in seen:
            continue
        for q in pts:
            if it.HitTest(q, pcbnew.FromMM(R)):
                seen.add(it.m_Uuid.AsString())
                print('     near:', desc(it), '' if it.GetNetname() == net else '  <-- other net')
                break
