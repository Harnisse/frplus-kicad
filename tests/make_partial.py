"""人手で一部を配線してから自動配線する、という使い方を再現したテスト基板を作る (KiCad 付属 python で実行)。

  python.exe make_partial.py <元の基板.kicad_pcb> <出力先フォルダ>

元の基板の手配線のうち、人が先に引くことが多い部分だけをロックして残し、残りのロックを外す
(frplus を unroute: true で動かすと、ロックされていない配線は消してから自動配線する)。
  - 電源と USB のネット (名前で判定)
  - ピン数の多い部品 (24 ピン以上) のパッドに端がある線と、そのパッドから 1 mm 以内のビア (ファンアウト)
元の基板は読むだけで変更しない。
"""
import os
import re
import shutil
import sys

import pcbnew

POWER = re.compile(r'^(\+|-)?(GND|V_?BAT|VBUS|VCC|VDD|VIN|\d+V\d*|\d+V\d*_\w+|3V3\w*|5V\w*|1V8\w*)$|USB_D', re.I)
MANY_PINS = 24
VIA_REACH = pcbnew.FromMM(1.0)


def main():
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(out, stem + '.kicad_pcb')
    shutil.copyfile(src, dst)
    for ext in ('.kicad_pro', '.kicad_dru'):
        if os.path.exists(os.path.splitext(src)[0] + ext):
            shutil.copyfile(os.path.splitext(src)[0] + ext, os.path.join(out, stem + ext))
    b = pcbnew.LoadBoard(dst)
    big = [p for fp in b.GetFootprints() if len(fp.Pads()) >= MANY_PINS for p in fp.Pads()]
    n = dict(power=0, fanout=0, via=0, free=0)
    for t in b.GetTracks():
        keep = False
        if POWER.search(t.GetNetname() or ''):
            keep, kind = True, 'power'
        elif t.Type() == pcbnew.PCB_VIA_T:
            pos = t.GetPosition()
            if any(p.GetNetname() == t.GetNetname() and (p.GetPosition() - pos).EuclideanNorm() <= VIA_REACH
                   for p in big):
                keep, kind = True, 'via'
        else:
            for end in (t.GetStart(), t.GetEnd()):
                if any(p.GetNetname() == t.GetNetname() and p.HitTest(end) for p in big):
                    keep, kind = True, 'fanout'
                    break
        t.SetLocked(keep)
        n[kind if keep else 'free'] += 1
    b.Save(dst)
    print('ロックして残す: 電源・USB %d、ファンアウトの線 %d、ファンアウトのビア %d。消して自動配線: %d'
          % (n['power'], n['fanout'], n['via'], n['free']))
    print(dst)


if __name__ == '__main__':
    main()
