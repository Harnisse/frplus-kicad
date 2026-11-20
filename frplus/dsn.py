"""DSN の前処理と、SES の結果を DSN へ書き戻す処理。座標は DSN の単位 (um、Y は下向きが負)。"""
import re

from .geom import inside
from .sexpr import block_end, child, num, parse, q

TOL = 2.0   # um。これ以内なら同じ点とみなす


def _wiring(dsn):
    i = dsn.index('(wiring')
    return i, block_end(dsn, i)


def wiring_items(dsn):
    """(wiring) の中身: [{'kind': 'wire'|'via', 'net', 'type', 'layer', 'width', 'pts' | 'padstack', 'x', 'y'}]"""
    i, e = _wiring(dsn)
    out = []
    for it in parse(dsn[i:e])[1:]:
        net = child(it, 'net')[1]
        typ = next((x[1] for x in it if isinstance(x, list) and x[0] == 'type'), 'route')
        if it[0] == 'wire':
            p = it[1]
            c = list(map(float, p[3:]))
            out.append(dict(kind='wire', net=net, type=typ, layer=p[1], width=float(p[2]),
                            pts=list(zip(c[0::2], c[1::2]))))
        elif it[0] == 'via':
            out.append(dict(kind='via', net=net, type=typ, padstack=it[1], x=float(it[2]), y=float(it[3])))
    return out


def _append_wiring(dsn, lines):
    if not lines:
        return dsn
    i, e = _wiring(dsn)
    return dsn[:e - 1].rstrip() + '\n' + '\n'.join(lines) + '\n  )' + dsn[e:]


def _wire_line(layer, width, a, b, net, typ):
    return '    (wire (path %s %s  %s %s  %s %s)(net %s)(type %s))' % (
        layer, num(width), num(a[0]), num(a[1]), num(b[0]), num(b[1]), q(net), typ)


def snap_to_pads(dsn, pads):
    """パッドの内側で止まっている配線の端点から、パッド中心までの短い線を足す。

    KiCad はパッドに触れていれば接続とみなすが、Freerouting は端点が中心に無いと未接続と数え、
    つながっているはずの線を引き直そうとする。pads は kicad_side.py の geom (mm 単位)。
    戻り値: (新しい DSN, 足した本数)
    """
    lines, seen = [], set()
    for w in wiring_items(dsn):
        if w['kind'] != 'wire':
            continue
        for x, y in (w['pts'][0], w['pts'][-1]):
            for p in pads:
                if p['net'] != w['net'] or w['layer'] not in p['layers']:
                    continue
                if not (p['x0'] * 1000 <= x <= p['x1'] * 1000 and p['y0'] * 1000 <= -y <= p['y1'] * 1000):
                    continue
                if len(p.get('poly') or ()) >= 3 and not inside(x / 1000, -y / 1000, p['poly']):
                    continue            # 外接矩形の中だが、パッドの銅の外
                cx, cy = round(p['x'] * 1000, 1), round(-p['y'] * 1000, 1)
                key = (w['layer'], x, y, cx, cy)
                if (abs(cx - x) > 1 or abs(cy - y) > 1) and key not in seen:
                    seen.add(key)
                    lines.append(_wire_line(w['layer'], w['width'], (x, y), (cx, cy), w['net'], w['type']))
    return _append_wiring(dsn, lines), len(lines)


def _on_seg(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / L2))
    return abs(px - a[0] - t * dx) <= TOL and abs(py - a[1] - t * dy) <= TOL


def merge_session(base_dsn, ses_text, keep='route', free=None):
    """base_dsn の (wiring) に、SES が持つ配線のうち base に無いものを keep 種別で追記した DSN を返す。

    free = ([(x, y), ...], 半径) を渡すと、その点から半径以内にかかる線分とビアだけは normal (引き剥がせる) にする。
    座標は DSN の単位 (um)。

    SES は固定配線をほとんど出力しないが、分割された固定配線の一部が混ざることがあるので、
    base の配線と重なる線分は捨てる。戻り値: (新しい DSN, 線分数, ビア数)
    """
    segs, vias = {}, {}
    for w in wiring_items(base_dsn):
        if w['kind'] == 'wire':
            segs.setdefault((w['net'], w['layer']), []).extend(zip(w['pts'], w['pts'][1:]))
        else:
            vias.setdefault(w['net'], []).append((w['x'], w['y']))
    routes = child(parse(ses_text), 'routes')
    scale = float(child(routes, 'resolution')[2])      # (resolution um 10) → 1/10 um 単位
    lines, nw, nv = [], 0, 0

    def typ(*pts):
        if free and any((px - fx) ** 2 + (py - fy) ** 2 <= free[1] ** 2 for px, py in pts for fx, fy in free[0]):
            return 'normal'
        return keep
    for net in child(routes, 'network_out')[1:]:
        name = net[1]
        for it in net[2:]:
            if it[0] == 'wire':
                p = it[1]
                layer, width = p[1], float(p[2]) / scale
                c = [float(v) / scale for v in p[3:]]
                pts = list(zip(c[0::2], c[1::2]))
                have = segs.get((name, layer), ())
                for a, b in zip(pts, pts[1:]):
                    probe = (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                    if all(any(_on_seg(px, py, s, t) for s, t in have) for px, py in probe):
                        continue
                    lines.append(_wire_line(layer, width, a, b, name, typ(*probe)))
                    nw += 1
            elif it[0] == 'via':
                x, y = float(it[2]) / scale, float(it[3]) / scale
                if any(abs(x - vx) <= TOL and abs(y - vy) <= TOL for vx, vy in vias.get(name, ())):
                    continue
                lines.append('    (via %s  %s %s (net %s)(type %s))' % (q(it[1]), num(x), num(y), q(name), typ((x, y))))
                nv += 1
    return _append_wiring(base_dsn, lines), nw, nv


# ---- ルールと層の読み取り (mm で返す) ---------------------------------------------
def _block(dsn, key):
    i = dsn.find('(%s' % key)
    return (i, block_end(dsn, i)) if i >= 0 else (None, None)


def add_pth_inner_shapes(dsn, pads, clearance, hole_clearance):
    """スルーホールのパッドの内層にも、穴の分の形を DSN に書き足す。戻り値: (新しい DSN, 足したパッド形状の数)

    KiCad の DSN 出力は、内層の銅を持たないスルーホールのパッド (内層の未接続部分を省く設定) の形を
    表と裏の層にしか書かない。Freerouting は内層ではそこに穴が無いものとして線を通し、KiCad の DRC で
    ショートになる (OpenDrone のコネクタ J16〜J19 で 11 件)。内層には「穴径 + 穴クリアランスの差」の円を置く。
    clearance と hole_clearance は mm。
    """
    a, b = _block(dsn, 'library')
    lib = parse(dsn[a:b])
    pin_stack = {}      # (部品形状名, ピン番号) → パッド形状名
    for img in (x for x in lib[1:] if isinstance(x, list) and x[0] == 'image'):
        for pin in (p for p in img[2:] if isinstance(p, list) and p[0] == 'pin'):
            name = next(t for t in pin[2:] if isinstance(t, str))
            pin_stack.setdefault((img[1], name.split('@')[0]), set()).add(pin[1])
    pa, pb = _block(dsn, 'placement')
    image_of = {}
    for comp in (x for x in parse(dsn[pa:pb])[1:] if isinstance(x, list) and x[0] == 'component'):
        for pl in comp[2:]:
            if isinstance(pl, list) and pl[0] == 'place':
                image_of[pl[1]] = comp[1]
    drill = {}          # パッド形状名 → 最大の穴径 (mm)
    for p in pads:
        if p.get('drill', 0) > 0:
            for st in pin_stack.get((image_of.get(p['ref']), p['num']), ()):
                drill[st] = max(drill.get(st, 0), p['drill'])
    layers = [n for n, t in re.findall(r'\(layer (\S+)\s*\(type (\w+)\)', dsn)]
    inner = layers[1:-1]
    if not drill or not inner:
        return dsn, 0
    count = 0
    for st, d in drill.items():
        m = re.search(r'\n(\s*)\(padstack %s\n' % re.escape(q(st)), dsn)
        if not m:
            continue
        s = m.start() + 1 + len(m.group(1))
        e = block_end(dsn, s)
        block = dsn[s:e]
        have = set(re.findall(r'\(shape \((?:polygon|circle|rect|path) (\S+)', block))
        missing = [l for l in inner if l not in have]
        if not missing:
            continue
        ind = m.group(1) + '  '
        # 内層の形は表の層と同じにする。KiCad の既定ではスルーホールのパッドは内層にも同じ大きさの銅を持つ
        # (OpenDrone のコネクタは 0.9x1.4 mm)。穴径の円では小さすぎて、内層の銅をかすめる線を防げなかった
        outer = None
        sm = re.search(r'\(shape \((polygon|circle|rect|path) (\S+)', block)
        if sm:
            se = block_end(block, sm.start())
            outer = (sm.group(2), block[sm.start():se])
        dia = max(d, d + 2 * (hole_clearance - clearance)) * 1000
        if outer:
            add = ''.join('%s%s\n' % (ind, outer[1].replace(' %s ' % outer[0], ' %s ' % l, 1)) for l in missing)
        else:
            add = ''.join('%s(shape (circle %s %s))\n' % (ind, l, num(dia)) for l in missing)
        i = block.index('\n' + ind + '(attach') + 1 if ('\n' + ind + '(attach') in block else block.rindex(')')
        dsn = dsn[:s] + block[:i] + add + block[i:] + dsn[e:]
        count += 1
    return dsn, count


def add_pad_clearances(dsn, pads, floor):
    """部品やパッドに個別に設定された、ネットクラスより大きいクリアランスを DSN に書き込む。

    KiCad の DSN 出力はこれを落とすので、Freerouting はネットクラスの間隔でパッドの脇を通してしまう。
    配置の (place ...) にピンごとのクリアランス区分を付け、構造の (rule ...) で区分の間隔を決める。
    floor (mm) 以下の値は書かない (ネットクラスより狭くはできない)。戻り値: (新しい DSN, {区分名: パッド数})
    """
    want = {}
    for p in pads:
        if p.get('clr', 0) > floor + 1e-6:
            want.setdefault(p['ref'], {})[p['num']] = round(p['clr'] * 1000)
    if not want:
        return dsn, {}
    a, b = _block(dsn, 'library')
    pins = {}       # 部品形状名 → DSN 上のピン名 (同じ番号のパッドが複数あると "29@1" のようになる)
    for img in (x for x in parse(dsn[a:b])[1:] if isinstance(x, list) and x[0] == 'image'):
        pins[img[1]] = [next(t for t in pin[2:] if isinstance(t, str))
                        for pin in img[2:] if isinstance(pin, list) and pin[0] == 'pin']
    a, b = _block(dsn, 'placement')
    image_of = {}
    for comp in (x for x in parse(dsn[a:b])[1:] if isinstance(x, list) and x[0] == 'component'):
        for pl in comp[2:]:
            if isinstance(pl, list) and pl[0] == 'place':
                image_of[pl[1]] = comp[1]
    classes, place = {}, dsn[a:b]
    for ref, nums in want.items():
        extra = []
        for name in pins.get(image_of.get(ref), ()):
            um = nums.get(name.split('@')[0])
            if um:
                cls = 'frclr%d' % um
                classes[cls] = classes.get(cls, 0) + 1
                extra.append('(pin %s (clearance_class %s))' % (q(name), cls))
        if extra:
            place = re.sub(r'(\(place %s [^\n]*)\)' % re.escape(q(ref)),
                           lambda m: m.group(1) + ' ' + ' '.join(extra) + ')', place, count=1)
    dsn = dsn[:a] + place + dsn[b:]
    # 最初の (rule は (structure の中にある。区分の無い (clearance N) は表全体を N で上書きするので、
    # 書き足す行は必ずその後ろ (規則の閉じ括弧の直前) に置く
    m = re.search(r'\n(\s*)\(rule\n', dsn)
    ind = m.group(1) + '  '
    end = block_end(dsn, m.start() + 1 + len(m.group(1))) - 1
    # 自分の (clearance) を持つネットクラスの線は、ネットクラス名の区分になる。そちらとの間隔も決める
    netclasses = re.findall(r'\n\s*\(class (\S+)', dsn)
    rules = ''.join('%s(clearance %s (type %s_%s))\n' % (ind, cls[5:], cls, k)
                    for cls in classes for k in ['wire', 'via', 'smd', 'pin'] + netclasses)
    head = dsn[:end].rstrip(' ')
    dsn = head + rules + m.group(1) + dsn[end:]
    return dsn, classes


def net_rules(dsn):
    """{ネット名: {'width', 'clearance', 'via_d', 'via_drill'}}。キー '' は既定クラス。"""
    out = {}
    for m in re.finditer(r'\(class ', dsn):
        c = parse(dsn[m.start():block_end(dsn, m.start())])
        rule = child(c, 'rule')
        r = dict(width=float(child(rule, 'width')[1]) / 1000, clearance=float(child(rule, 'clearance')[1]) / 1000,
                 via_d=0.6, via_drill=0.3)
        try:        # KiCad のビア名は "Via[0-3]_600:300_um" (直径:穴径)
            d = re.search(r'_(\d+):(\d+)_um', child(child(c, 'circuit'), 'use_via')[1])
            r.update(via_d=int(d.group(1)) / 1000, via_drill=int(d.group(2)) / 1000)
        except StopIteration:
            pass
        for net in (x for x in c[2:] if isinstance(x, str)):
            out[net] = r
        if c[1] == 'kicad_default' or '' not in out:
            out[''] = r
    return out


def signal_layers(dsn):
    """信号を引ける層の名前 (power 層を除く)。"""
    return [n for n, t in re.findall(r'\(layer (\S+)\s*\(type (\w+)\)', dsn) if t == 'signal']


def outline_boxes(dsn):
    """基板外形の外接矩形のリスト [(x0, y0, x1, y1), ...]、KiCad の座標系 (Y は下向きが正)。

    外形 1 つにつき 1 つ。分割キーボードのように 1 枚に複数の基板を並べた設計では、
    KiCad は 1 つの (boundary ...) の中に (path ...) を外形の数だけ書く (Ferris 0.1 全基板版で確認)。
    """
    boxes = []
    for m in re.finditer(r'\(boundary\b', dsn):
        for b in parse(dsn[m.start():block_end(dsn, m.start())])[1:]:
            if not isinstance(b, list) or b[0] not in ('path', 'polygon', 'rect'):
                continue
            c = [float(v) / 1000 for v in (b[2:] if b[0] == 'rect' else b[3:])]
            xs, ys = c[0::2], [-v for v in c[1::2]]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def add_keepouts(dsn, rects):
    """rects: [{'layer', 'x0', 'y0', 'x1', 'y1'}] (mm、KiCad の座標系) を配線・ビア禁止領域として足す。

    'poly': [(x, y), ...] があれば、矩形ではなくその多角形を使う (基板内側のくり抜き)。
    """
    if not rects:
        return dsn
    lines = []
    for r in rects:
        if r.get('poly'):
            pts = list(r['poly']) + [r['poly'][0]]
            lines.append('    (keepout "" (polygon %s 0  %s))'
                         % (r['layer'], '  '.join('%s %s' % (num(x * 1000), num(-y * 1000)) for x, y in pts)))
            continue
        x0, y0, x1, y1 = (num(v * 1000) for v in (r['x0'], -r['y0'], r['x1'], -r['y1']))
        lines.append('    (keepout "" (polygon %s 0  %s %s  %s %s  %s %s  %s %s  %s %s))'
                     % (r['layer'], x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))
    i = dsn.index('(boundary')
    e = block_end(dsn, i)
    return dsn[:e] + '\n' + '\n'.join(lines) + dsn[e:]


def strip_planes(dsn, nets):
    """nets のベタ定義 (plane) を DSN から外す。戻り値: (新しい DSN, 外した数)

    Freerouting はベタを持つネットを「ベタでつながる」とみなして配線を省く。ベタが配線で分断されて
    実際にはつながらなかったネットは、ベタ定義を外して普通のネットとして引き直させる。
    """
    # power 層 (ベタ専用の層) のベタは外さない。外すとその層が空になり、Freerouting がそこへ信号を引く
    # (OpenDrone で V_BAT の In1.Cu を外したら、信号線が 206 本引かれた)
    power = {n for n, t in re.findall(r'\(layer (\S+)\s*\(type (\w+)\)', dsn) if t == 'power'}
    nets, out, pos, n = set(nets), [], 0, 0
    for m in re.finditer(r'\(plane ', dsn):
        e = block_end(dsn, m.start())
        node = parse(dsn[m.start():e])
        shape = next((x for x in node[2:] if isinstance(x, list)), [None, None])
        if node[1] in nets and shape[1] not in power:
            out.append(dsn[pos:m.start()])
            pos = e
            n += 1
    return ''.join(out) + dsn[pos:], n


def enforce_minimums(dsn, min_track_um, min_clearance_um):
    """ネットクラスの線幅・クリアランスが基板の最小値を下回っていたら、最小値まで引き上げる。

    KiCad ではネットクラスと基板の制約が別々に設定でき、食い違ったままの基板がある。Freerouting は
    ネットクラスにしか従わないので、そのままだと DRC 違反だらけになる。戻り値: (新しい DSN, 変更の説明)
    """
    notes = set()

    def fix(kind, floor):
        def rep(m):
            v = float(m.group(1))
            if v >= floor:
                return m.group(0)
            notes.add('%s %.2f → %.2f mm' % ('線幅' if kind == 'width' else 'クリアランス', v / 1000, floor / 1000))
            return '(%s %s)' % (kind, num(floor))
        return rep

    dsn = re.sub(r'\(width ([\d.]+)\)', fix('width', min_track_um), dsn)
    dsn = re.sub(r'\(clearance ([\d.]+)\)', fix('clearance', min_clearance_um), dsn)   # (type ...) 付きは対象外
    return dsn, sorted(notes)


def resize_vias(dsn, clearance, hole_clearance, min_via, min_annular):
    """ビアの外径を基板の制約に合わせる (引数はすべて mm)。戻り値: (新しい DSN, 実寸の変更 {元 um: 新 um}, 仮の拡大 {仮 um: 実寸 um})

    1. 実寸: 最小ビア径と最小アニュラー幅を満たすまで広げる。取り込んだ後もこの寸法になる。
    2. 仮の拡大: Freerouting は銅箔どうしの間隔しか見ないので、ビアの銅の幅が小さいと、穴と他ネットの
       銅の間隔が穴クリアランスを下回る。DSN の中だけ外径を「穴径 + 2 x (穴クリアランス - 銅箔クリアランス)」
       まで広げて配線させ、取り込み時に実寸へ戻す。
    """
    real, virtual, out, pos = {}, {}, [], 0
    for m in re.finditer(r'\(padstack "?Via\[[^\]]*\]_(\d+):(\d+)_um"?', dsn):
        d, hole = int(m.group(1)), int(m.group(2))
        want = max(d, int(round(min_via * 1000)), int(round(hole + 2 * min_annular * 1000)))
        need = max(want, int(round(hole + 2 * (hole_clearance - clearance) * 1000)))
        if need == d:
            continue
        e = block_end(dsn, m.start())
        out.append(dsn[pos:m.start()])
        out.append(re.sub(r'\(circle (\S+) %d\)' % d, lambda c: '(circle %s %d)' % (c.group(1), need), dsn[m.start():e]))
        pos = e
        if want != d:
            real[d] = want
        if need != want:
            virtual[need] = want
    return ''.join(out) + dsn[pos:], real, virtual
