"""基板の概要を表示する (KiCad 付属 python で実行): board_info.py <board.kicad_pcb>"""
import collections
import sys

import pcbnew

b = pcbnew.LoadBoard(sys.argv[1])
mm = pcbnew.ToMM
bb = b.GetBoardEdgesBoundingBox()
cu = [b.GetLayerName(l) for l in b.GetEnabledLayers().CuStack()]
print('size %.1f x %.1f mm, copper layers %s' % (mm(bb.GetWidth()), mm(bb.GetHeight()), cu))
tr = list(b.GetTracks())
vias = [t for t in tr if t.Type() == pcbnew.PCB_VIA_T]
print('footprints %d, pads %d, nets %d, segments %d, vias %d, locked %d' % (
    len(b.GetFootprints()), sum(len(f.Pads()) for f in b.GetFootprints()), b.GetNetCount(),
    len(tr) - len(vias), len(vias), sum(1 for t in tr if t.IsLocked())))
print('track widths', collections.Counter(round(mm(t.GetWidth()), 3) for t in tr if t.Type() != pcbnew.PCB_VIA_T).most_common(8))
print('via sizes', collections.Counter((round(mm(v.GetWidth(pcbnew.F_Cu)), 2), round(mm(v.GetDrillValue()), 2), v.GetViaType()) for v in vias).most_common(5))
print('track layers', collections.Counter(t.GetLayerName() for t in tr if t.Type() != pcbnew.PCB_VIA_T).most_common())
for z in b.Zones():
    print('zone', repr(z.GetNetname()), [b.GetLayerName(l) for l in z.GetLayerSet().Seq()], 'rule' if z.GetIsRuleArea() else '', 'prio', z.GetAssignedPriority())
ds = b.GetDesignSettings()
print('min clearance %.3f, min track %.3f, edge clr %.3f, min via %.2f/%.2f' % (mm(ds.m_MinClearance), mm(ds.m_TrackMinWidth), mm(ds.m_CopperEdgeClearance), mm(ds.m_ViasMinSize), mm(ds.m_MinThroughDrill)))
