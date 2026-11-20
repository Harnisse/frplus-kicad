"""人手で配線された元の基板と、自動配線の結果を比べる (KiCad 付属 python で実行)。

  python.exe compare.py <元の基板> <自動配線の結果>
"""
import collections
import sys

import pcbnew

mm = pcbnew.ToMM


def stats(path):
    b = pcbnew.LoadBoard(path)
    length = collections.Counter()
    width = collections.Counter()
    per_net = collections.Counter()
    vias = 0
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            vias += 1
            continue
        L = mm(t.GetLength())
        length[t.GetLayerName()] += L
        width[round(mm(t.GetWidth()), 2)] += L
        per_net[t.GetNetname()] += L
    return dict(length=length, width=width, per_net=per_net, vias=vias, total=sum(length.values()))


def main(orig, routed):
    a, b = stats(orig), stats(routed)
    print('%-22s %10s %10s' % ('', '元 (人手)', '自動'))
    print('%-22s %10.0f %10.0f  (%+.0f%%)' % ('配線長の合計 mm', a['total'], b['total'],
                                           100 * (b['total'] - a['total']) / max(a['total'], 1)))
    print('%-22s %10d %10d  (%+.0f%%)' % ('ビア数', a['vias'], b['vias'], 100 * (b['vias'] - a['vias']) / max(a['vias'], 1)))
    for l in sorted(set(a['length']) | set(b['length'])):
        print('  %-20s %10.0f %10.0f' % (l, a['length'][l], b['length'][l]))
    print('線幅ごとの配線長 mm')
    for w in sorted(set(a['width']) | set(b['width'])):
        print('  %-20s %10.0f %10.0f' % ('%.2f mm' % w, a['width'][w], b['width'][w]))
    worse = sorted(((b['per_net'][n] / a['per_net'][n], n) for n in a['per_net'] if a['per_net'][n] > 5 and b['per_net'][n] > 0),
                   reverse=True)[:8]
    print('元より長くなったネット (倍率, 元 mm → 自動 mm)')
    for r, n in worse:
        print('  x%.1f  %-28s %6.1f → %6.1f' % (r, n, a['per_net'][n], b['per_net'][n]))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
