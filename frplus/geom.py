"""基板上の形状を使う前処理: 出口の無いパッドの検出と、電源ピンから内層ベタへのビア配置。単位は mm。

障害物は「他ネットの銅」だけを見る (パッド、ロック済みの配線とビア)。ロックされていない配線は
Freerouting が引き剥がせるので障害物に数えない。パッドは外接矩形で近似する。
"""
import heapq
import math

BUCKET = 2.0        # 障害物を引くための格子 (mm)
SLACK = 0.01        # 計算上ちょうど通る隙間は、実際の配線器では通らない


class Obstacle:
    """kind: 'rect' (x0, y0, x1, y1) | 'seg' (x1, y1, x2, y2, r) | 'circle' (x, y, r) | 'poly' (x0, y0, x1, y1, pts)"""
    __slots__ = ('kind', 'g', 'layers', 'net', 'label', 'locked')

    def __init__(self, kind, g, layers, net, label, locked):
        self.kind, self.g, self.layers, self.net, self.label, self.locked = kind, g, frozenset(layers), net, label, locked

    def dist(self, x, y, limit=0.0):
        """点から銅の縁までの距離 (内側なら 0)。limit 以上と分かった時点で、それ以上正確には求めない。"""
        g = self.g
        if self.kind == 'rect':
            return math.hypot(max(g[0] - x, 0.0, x - g[2]), max(g[1] - y, 0.0, y - g[3]))
        if self.kind == 'poly':
            d = math.hypot(max(g[0] - x, 0.0, x - g[2]), max(g[1] - y, 0.0, y - g[3]))
            if d > 0 and d >= limit:
                return d                # 外接矩形からでも十分遠い
            pts = g[4]
            if inside(x, y, pts):
                return 0.0
            return min(_seg_dist(x, y, pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))
        if self.kind == 'circle':
            return max(0.0, math.hypot(x - g[0], y - g[1]) - g[2])
        dx, dy = g[2] - g[0], g[3] - g[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - g[0]) * dx + (y - g[1]) * dy) / L2))
        return max(0.0, math.hypot(x - g[0] - t * dx, y - g[1] - t * dy) - g[4])

    def bbox(self):
        g = self.g
        if self.kind in ('rect', 'poly'):
            return g[:4]
        if self.kind == 'circle':
            return g[0] - g[2], g[1] - g[2], g[0] + g[2], g[1] + g[2]
        return min(g[0], g[2]) - g[4], min(g[1], g[3]) - g[4], max(g[0], g[2]) + g[4], max(g[1], g[3]) + g[4]


class Board:
    def __init__(self, geo, rules, signal_layers, outline, edge_clearance):
        """geo: kicad_side.prepare の結果。rules: {net: {'width', 'clearance', 'via_d', 'via_drill'}} (mm)。"""
        self.geo, self.rules, self.signal = geo, rules, list(signal_layers)
        self.outline, self.edge = list(outline), edge_clearance  # outline: [(x0, y0, x1, y1), ...] 外形ごと
        self.grid = {}
        self.all_cu = sorted({l for p in geo['pads'] for l in p['layers']})
        self.pad_obs = {}
        for p in geo['pads']:
            box = (p['x0'], p['y0'], p['x1'], p['y1'])
            label = 'パッド %s-%s' % (p['ref'], p['num'])
            if len(p.get('poly') or ()) >= 3:
                o = Obstacle('poly', box + (p['poly'],), p['layers'], p['net'], label, False)
            else:
                o = Obstacle('rect', box, p['layers'], p['net'], label, False)
            self.pad_obs[(p['ref'], p['num'], p['x'], p['y'])] = o
            self.add(o)
        for t in geo['tracks']:
            if t['locked']:
                self.add_track(t)
        for v in geo['vias']:
            if v['locked']:
                self.add_via(v)

    def add(self, o):
        x0, y0, x1, y1 = o.bbox()
        for i in range(int(x0 // BUCKET), int(x1 // BUCKET) + 1):
            for j in range(int(y0 // BUCKET), int(y1 // BUCKET) + 1):
                self.grid.setdefault((i, j), []).append(o)

    def add_track(self, t):
        self.add(Obstacle('seg', (t['x1'], t['y1'], t['x2'], t['y2'], t['w'] / 2), [t['layer']], t['net'],
                          'ロック済み配線 [%s]' % t['net'], True))

    def add_via(self, v):
        self.add(Obstacle('circle', (v['x'], v['y'], v['d'] / 2), self.all_cu, v['net'],
                          'ロック済みビア [%s]' % v['net'], True))

    def pad(self, p):
        """geo のパッドに対応する障害物。"""
        return self.pad_obs[(p['ref'], p['num'], p['x'], p['y'])]

    def rule(self, net):
        return self.rules.get(net) or self.rules['']

    def near(self, x, y, reach):
        seen = set()
        for i in range(int((x - reach) // BUCKET), int((x + reach) // BUCKET) + 1):
            for j in range(int((y - reach) // BUCKET), int((y + reach) // BUCKET) + 1):
                for o in self.grid.get((i, j), ()):
                    if id(o) not in seen:
                        seen.add(id(o))
                        yield o

    def blocker(self, x, y, r, layers, net):
        """半径 r の銅を (x, y) に置けない理由になる障害物を返す。置けるなら None。'端' は基板端。"""
        clr = self.rule(net)['clearance']
        m = r + self.edge
        if not any(x0 + m <= x <= x1 - m and y0 + m <= y <= y1 - m for x0, y0, x1, y1 in self.outline):
            return '端'
        need = r + clr + SLACK
        for o in self.near(x, y, need):
            if o.net != net and not o.layers.isdisjoint(layers) and o.dist(x, y, need) < need:
                return o
        return None

    def via_ok(self, x, y, net):
        """(x, y) にビアを打てるか。

        Freerouting はビアと「同じネットの」パッド・ビアの間にもネットクラスのクリアランスを要求する
        (KiCad の DRC では違反にならないが、Freerouting はそこにビアを打たない。設定では変えられない)。
        """
        rule = self.rule(net)
        vr = rule['via_d'] / 2
        if self.blocker(x, y, vr, self.all_cu, net) is not None:
            return False
        need = vr + rule['clearance'] + SLACK
        return not any(o.kind != 'seg' and o.dist(x, y, need) < need for o in self.near(x, y, need))

    def in_zone(self, x, y, net, layer, margin):
        """(x, y) が layer 上で net のベタの中にあるか。優先度の高い他ネットのベタに食われていれば False。"""
        own = [z for z in self.geo['zones'] if not z['rule_area'] and z['net'] == net and layer in z['layers']]
        pts = [(x, y), (x - margin, y), (x + margin, y), (x, y - margin), (x, y + margin)]
        for z in own:
            if all(inside(px, py, z['pts']) for px, py in pts):
                for o in self.geo['zones']:
                    if (not o['rule_area'] and o['net'] != net and layer in o['layers']
                            and o['priority'] > z['priority'] and any(inside(px, py, o['pts']) for px, py in pts)):
                        break
                else:
                    return True
        return False


def _seg_dist(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
    return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)


def inside(x, y, pts):
    c, n = False, len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


# ---- 出口の無いパッド ------------------------------------------------------------
def trapped_pads(board, step=0.05, radius=2.5):
    """固定の障害物だけで出口を塞がれているパッドを探す。

    パッドから線幅ぶんの銅を動かしていき (必要ならビアで層を替える)、パッド中心から radius だけ
    離れられれば「出口あり」とする。戻り値: [{'pad', 'net', 'blockers': {説明: 回数}, 'nets': [ロック済みの相手ネット]}]
    """
    count = {}
    for p in board.geo['pads']:
        count[p['net']] = count.get(p['net'], 0) + 1
    plane = {z['net'] for z in board.geo['zones'] if not z['rule_area']
             and any(l not in board.signal for l in z['layers'])}
    out = []
    for p in board.geo['pads']:
        if not p['net'] or count[p['net']] < 2 or p.get('poured'):
            continue                    # 接続先が無い、またはベタ経由でつながっている
        layers = [l for l in p['layers'] if l in board.signal]
        if not layers or (p['net'] in plane and len(p['layers']) > len(layers)):
            continue                    # 内層ベタに直接つながるスルーホール
        r = _escape(board, p, layers, step, radius)
        if r is not None:
            out.append(r)
    return out


def _escape(board, pad, layers, step, radius):
    net = pad['net']
    rule = board.rule(net)
    hw = rule['width'] / 2
    cx, cy = pad['x'], pad['y']
    # 細いパッドから出る線はパッド幅まで細くなる (ネックダウン) ので、パッドより太い線では判定しない
    hw = min(hw, pad.get('min_dim', min(pad['x1'] - pad['x0'], pad['y1'] - pad['y0'])) / 2)
    own = board.pad(pad)
    seen, blockers = set(), {}
    heap = [(0.0, 0, 0, l) for l in layers]
    while heap:
        negd, i, j, layer = heapq.heappop(heap)
        if (i, j, layer) in seen:
            continue
        seen.add((i, j, layer))
        x, y = cx + i * step, cy + j * step
        inpad = layer in pad['layers'] and own.dist(x, y) == 0
        if not inpad:
            b = board.blocker(x, y, hw, [layer], net)
            if b is not None:
                blockers[b] = blockers.get(b, 0) + 1
                continue
        if -negd >= radius:
            return None                 # 出口あり
        if not inpad and any(o.net == net and layer in o.layers and o is not own and o.dist(x, y) == 0
                             for o in board.near(x, y, 0.01)):
            return None                 # 同じネットの別の銅 (隣のパッドなど) に届いた
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = i + di, j + dj
            if (ni, nj, layer) not in seen:
                heapq.heappush(heap, (-math.hypot(ni * step, nj * step), ni, nj, layer))
        if not inpad and board.via_ok(x, y, net):
            for l in board.signal:      # ここにビアを打てる
                if (i, j, l) not in seen:
                    heapq.heappush(heap, (negd, i, j, l))
    named = {}
    for b, n in blockers.items():
        key = b if isinstance(b, str) else b.label
        named[key] = named.get(key, 0) + n
    nets = sorted({b.net for b in blockers if not isinstance(b, str) and b.locked})
    return dict(pad='%s-%s' % (pad['ref'], pad['num']), net=net, blockers=named, nets=nets)


# ---- 電源ピンから内層ベタへのビア ------------------------------------------------
def plane_fanout(board, max_dist=2.5, step=0.1):
    """内層にベタを持つネットの表面実装パッドのうち、まだ何もつながっていないものにビアと短い線を足す。

    戻り値: (tracks, vias, failed)。足したものは board の障害物にも加える。
    """
    zones = [z for z in board.geo['zones'] if not z['rule_area']]
    plane = {}
    for z in zones:
        for l in z['layers']:
            if l not in board.signal:
                plane.setdefault(z['net'], set()).add(l)
    ends = {}
    for t in board.geo['tracks']:
        for x, y in ((t['x1'], t['y1']), (t['x2'], t['y2'])):
            ends.setdefault((t['net'], t['layer']), []).append((x, y))
    tracks, vias, failed = [], [], []
    for p in board.geo['pads']:
        net = p['net']
        if net not in plane or len(p['layers']) != 1 or p.get('poured'):
            continue                    # 対象外、またはすでに同じ層のベタにつながっている
        layer = p['layers'][0]
        if any(board.pad(p).dist(x, y) == 0 for x, y in ends.get((net, layer), ())):
            continue                    # すでに配線が出ている
        rule = board.rule(net)
        vr = rule['via_d'] / 2
        w = min(rule['width'], p['x1'] - p['x0'], p['y1'] - p['y0'])
        best = None
        d = step
        while d <= max_dist and best is None:
            for k in range(24):
                a = k * math.pi / 12
                x, y = round(p['x'] + d * math.cos(a), 3), round(p['y'] + d * math.sin(a), 3)
                if not all(board.in_zone(x, y, net, l, vr + 0.3) for l in plane[net]):
                    continue                                        # 自分のベタの外では内層につながらない
                if not board.via_ok(x, y, net):
                    continue
                if any(math.hypot(x - v['x'], y - v['y']) < v['d'] / 2 + vr + rule['clearance'] + SLACK
                       for v in board.geo['vias']):                 # ロックされていない既存ビアとも離す
                    continue
                n = max(1, int(d / 0.05))
                if any(board.blocker(p['x'] + (x - p['x']) * i / n, p['y'] + (y - p['y']) * i / n, w / 2, [layer], net)
                       is not None for i in range(n + 1)):
                    continue                                        # 線が他ネットに近すぎる
                best = (x, y)
                break
            d += step
        if best is None:
            failed.append('%s-%s [%s]' % (p['ref'], p['num'], net))
            continue
        t = dict(net=net, layer=layer, w=w, x1=p['x'], y1=p['y'], x2=best[0], y2=best[1], locked=True)
        v = dict(net=net, x=best[0], y=best[1], d=rule['via_d'], drill=rule['via_drill'], locked=True)
        tracks.append(t)
        vias.append(v)
        board.add_track(t)
        board.add_via(v)
    return tracks, vias, failed
