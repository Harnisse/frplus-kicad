"""基板の読み書き (KiCad 付属の python で実行する。pcbnew に触るのはこのファイルだけ)。

  python.exe kicad_side.py <args.json>

args.json: {"cmd": "prepare" | "add" | "import" | "remove", ...}。結果は args["result"] の JSON に書く。
基板ファイルはその場で書き換えるので、呼び出し側が作業用コピーを渡すこと。
"""
import json
import sys

import pcbnew

mm = pcbnew.ToMM
FromMM = pcbnew.FromMM


_removed = []      # 消した配線を Python 側で解放させない (解放されると後続の pcbnew 呼び出しが落ちる)


def _load(path):
    """基板とゾーン一覧を返す。Zones() は配線を消した後に呼ぶと落ちることがあるので、読み込み直後に取る。"""
    board = pcbnew.LoadBoard(path)
    return board, board.Zones()


def _remove(board, item):
    board.Remove(item)
    _removed.append(item)


def _refill(board, zones):
    pcbnew.ZONE_FILLER(board).Fill(zones)


def _unique_refs(board):
    """参照名が重複する部品の 2 つ目以降を、一意の名前に一時的に変える。戻り値: [(部品, 元の参照名)]。

    KiCad の DSN 出力は参照名が重複すると何も言わずに失敗する (ロゴの部品が "G***" のまま複数ある基板など)。
    SES の取り込みも参照名で部品を探すので、取り込みの前にも同じ名前に変え、保存の前に元へ戻すこと。
    並び順で決まるので、同じ基板なら毎回同じ名前になる。
    """
    names = {fp.GetReference() for fp in board.GetFootprints()}
    used, renamed = set(), []
    for fp in board.GetFootprints():
        r = fp.GetReference()
        if r in used:
            n = 2
            while '%s_frplus%d' % (r, n) in names:
                n += 1
            names.add('%s_frplus%d' % (r, n))
            fp.SetReference('%s_frplus%d' % (r, n))
            renamed.append((fp, r))
        used.add(r)
    return renamed


def _export_dsn(board, zones, path):
    """DSN を書き出す。必ず保存や形状の読み取りを済ませた最後に呼ぶこと (基板オブジェクトを書き換える)。

    KiCad は、配線もビアも禁止していないルールエリア (多チャンネル設計の配置用エリアなど) まで
    配線禁止領域として DSN に出す。そのままだと Freerouting がその中に何も引けなくなるので外す。
    """
    dropped = 0
    for z in zones:
        if z.GetIsRuleArea() and not z.GetDoNotAllowTracks() and not z.GetDoNotAllowVias():
            _remove(board, z)
            dropped += 1
    npth = _widen_npth(board)          # 書き出しより先に広げる
    return bool(pcbnew.ExportSpecctraDSN(board, path)), dropped, npth


def _widen_npth(board):
    """パッド径が穴径より大きい NPTH (キースイッチの位置決め穴など) を、穴クリアランスを守れる大きさに広げる。

    KiCad はこうした NPTH を銅のあるピンとして DSN に出すので、Freerouting は銅箔のクリアランスしか空けない。
    KiCad の DRC は穴の縁から穴クリアランスを求めるので、その差だけ線が近づきすぎる (Lily58 Lite Rev3 で
    穴 1.9 mm・パッド 1.91 mm の穴に 21 件)。パッド径が穴径以下の NPTH は KiCad が配線禁止として出すので触らない。
    DSN の書き出し専用 (基板オブジェクトを書き換えるので、保存の後に呼ぶ)。戻り値: 広げたパッドの数。
    """
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    clr = min([ns.GetDefaultNetclass().GetClearance()] + [c.GetClearance() for c in ns.GetNetclasses().values()])
    extra = 2 * (ds.m_HoleClearance - clr)
    if extra <= 0:
        return 0
    n = 0
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                continue
            size, drill = p.GetSize(), p.GetDrillSize()
            if size.x <= drill.x and size.y <= drill.y:
                continue
            want = pcbnew.VECTOR2I(max(size.x, drill.x + extra), max(size.y, drill.y + extra))
            if want.x != size.x or want.y != size.y:
                p.SetSize(want)
                n += 1
    return n


def _copper_layers(board, item):
    return [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack() if item.IsOnLayer(l)]


def _own_pad_vias(board):
    """既存のビアのうち、同じネットの表面実装パッドの中にあるもの、すぐ隣 (クリアランス未満) にあるものを数える。
    Freerouting は既定ではこうした位置にビアを打てないので、設計者がその流儀かどうかを配線を消す前に見ておく。"""
    import math
    pads = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                if p.IsOnLayer(layer):
                    ps = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(ps, layer, 0, 5000, pcbnew.ERROR_INSIDE)
                    if ps.OutlineCount():
                        o = ps.Outline(0)
                        pads.setdefault(p.GetNetCode(), []).append(
                            [(o.CPoint(i).x, o.CPoint(i).y) for i in range(o.PointCount())])

    def dist(pts, x, y):
        inside, d = False, 1e18
        for i in range(len(pts)):
            (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
            dx, dy = x2 - x1, y2 - y1
            t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / ((dx * dx + dy * dy) or 1)))
            d = min(d, math.hypot(x - x1 - t * dx, y - y1 - t * dy))
        return -d if inside else d

    clr = board.GetDesignSettings().m_MinClearance
    total = inpad = near = 0
    for v in board.GetTracks():
        if v.Type() != pcbnew.PCB_VIA_T or v.GetNetCode() not in pads:
            continue
        total += 1
        x, y, r = v.GetPosition().x, v.GetPosition().y, v.GetWidth(pcbnew.F_Cu) / 2
        g = min(dist(pts, x, y) for pts in pads[v.GetNetCode()])
        if g < 0:
            inpad += 1
        elif g - r < clr - 1000:
            near += 1
    return dict(total=total, in_pad=inpad, near_pad=near)


def prepare(a):
    """作業用コピーを配線前の状態に整え、DSN とパッド情報を書き出す。"""
    board, zones = _load(a['pcb'])
    own_pad_vias = _own_pad_vias(board)
    release = set(a.get('release_nets') or ())
    released = unrouted = 0
    for t in list(board.GetTracks()):
        if t.IsLocked():
            if t.GetNetname() in release:       # 明示的に指定されたネットだけ固定を外して引き直させる
                _remove(board, t)
                released += 1
        elif a.get('unroute'):
            _remove(board, t)
            unrouted += 1
    for name in a.get('plane_layers') or ():    # ベタ専用層。power 層にすると Freerouting が信号を引かない
        board.SetLayerType(board.GetLayerID(name), pcbnew.LT_POWER)
    _refill(board, zones)
    board.Save(a['pcb'])
    renamed = _unique_refs(board)       # 保存の後に変える (作業用コピーの参照名は元のまま)
    pads = []
    fills = {}          # (ネット, 層 ID) → その層に塗りを持つゾーン
    for z in zones:
        if not z.GetIsRuleArea():
            for l in z.GetLayerSet().Seq():
                fills.setdefault((z.GetNetname(), l), []).append(z)
    for fp in board.GetFootprints():
        for p in fp.Pads():
            bb = p.GetBoundingBox()
            # ベタの塗りがパッドの銅に届いていれば、そのパッドはベタ経由でつながっている
            reach = min(bb.GetWidth(), bb.GetHeight()) // 2 + FromMM(0.05)
            poured = any(z.HitTestFilledArea(l, p.GetPosition(), reach)
                         for l in board.GetEnabledLayers().CuStack() if p.IsOnLayer(l)
                         for z in fills.get((p.GetNetname(), l), ()))
            outline = []
            cu = [l for l in board.GetEnabledLayers().CuStack() if p.IsOnLayer(l)]
            if cu:                              # 回転した部品や角丸のパッドを正しく扱うため、実際の外形を渡す
                o = p.GetEffectivePolygon(cu[0], pcbnew.ERROR_INSIDE).Outline(0)
                outline = [(mm(o.CPoint(i).x), mm(o.CPoint(i).y)) for i in range(o.PointCount())]
            # 部品やパッドに個別に設定したクリアランス。KiCad の DSN 出力には載らない
            clr = p.GetLocalClearance()
            if clr is None:
                clr = fp.GetLocalClearance()
            pads.append(dict(ref=fp.GetReference(), num=p.GetNumber(), net=p.GetNetname(), poly=outline,
                             clr=mm(clr) if clr else 0,
                             min_dim=mm(min(p.GetSize().x, p.GetSize().y)),
                             x=mm(p.GetPosition().x), y=mm(p.GetPosition().y),
                             x0=mm(bb.GetLeft()), y0=mm(bb.GetTop()), x1=mm(bb.GetRight()), y1=mm(bb.GetBottom()),
                             layers=_copper_layers(board, p), drill=mm(p.GetDrillSize().x), poured=poured))
    tracks, vias = [], []
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            vias.append(dict(net=t.GetNetname(), x=mm(t.GetPosition().x), y=mm(t.GetPosition().y),
                             d=mm(t.GetWidth(pcbnew.F_Cu)), drill=mm(t.GetDrillValue()), locked=t.IsLocked()))
        else:
            tracks.append(dict(net=t.GetNetname(), layer=t.GetLayerName(), w=mm(t.GetWidth()), locked=t.IsLocked(),
                               x1=mm(t.GetStart().x), y1=mm(t.GetStart().y), x2=mm(t.GetEnd().x), y2=mm(t.GetEnd().y)))
    zlist = []
    for z in zones:
        o = z.Outline().Outline(0)
        zlist.append(dict(net=z.GetNetname(), rule_area=bool(z.GetIsRuleArea()), priority=z.GetAssignedPriority(),
                          layers=_copper_layers(board, z),
                          pts=[(mm(o.CPoint(i).x), mm(o.CPoint(i).y)) for i in range(o.PointCount())]))
    # Freerouting に見えていない障害物: パッド以外のレジスト開口 (ロゴや露出パターン) と、
    # 銅箔層に置かれた文字・図形。上を他ネットの線が通ると DRC エラーになる
    openings = []
    shapes = [(g, None) for g in board.GetDrawings()]
    shapes += [(g, fp) for fp in board.GetFootprints() for g in fp.GraphicalItems()]
    cu_pads = {}                        # 銅箔層 → その層のパッドの外接矩形
    for fp in board.GetFootprints():
        for p in fp.Pads():
            for l in board.GetEnabledLayers().CuStack():
                if p.IsOnLayer(l):
                    pb = p.GetBoundingBox()
                    pb.Inflate(FromMM(0.1))     # レジスト開口はパッドより少し大きく描かれる (OpenDeck の USB-C は片側 0.05 mm)
                    cu_pads.setdefault(l, []).append(pb)
    mask_on_pads = 0
    for g, fp in shapes:
        is_mask = g.GetLayer() in (pcbnew.F_Mask, pcbnew.B_Mask)
        cu = {pcbnew.F_Mask: pcbnew.F_Cu, pcbnew.B_Mask: pcbnew.B_Cu}.get(g.GetLayer())
        if cu is None and pcbnew.IsCopperLayer(g.GetLayer()):
            cu = g.GetLayer()
        if cu is not None:
            bb = g.GetBoundingBox()
            # パッドの上に図形で描いたレジスト開口 (パッド自身が F.Mask を持たない部品、OpenDeck の USB-C など) は、
            # そのパッドのための開口なので禁止にしない。禁止にすると Freerouting がパッドにつなげなくなる。
            # 1 つのパッドにほぼ収まるものだけ。はんだジャンパのように 2 つのパッドの隙間にまたがる開口は禁止のまま
            if is_mask and bb.GetArea() > 0:
                covered = max([(bb.Intersect(pb).GetArea() if bb.Intersects(pb) else 0) for pb in cu_pads.get(cu, [])] or [0])
                if covered >= 0.9 * bb.GetArea():
                    mask_on_pads += 1
                    continue
            openings.append(dict(layer=board.GetLayerName(cu), x0=mm(bb.GetLeft()), y0=mm(bb.GetTop()),
                                 x1=mm(bb.GetRight()), y1=mm(bb.GetBottom())))
    # 基板の内側のくり抜き (フットプリントが持つ穴を含む)。Freerouting の基板端クリアランスは
    # 外周にしか効かないので、クリアランスぶん広げた配線禁止領域にする。
    # くり抜きは KiCad が組み立てた外形ポリゴンの「穴」だけから作る。外形線の図形ごとに外接矩形で判定すると、
    # 曲線や凹みのある外形の辺 (キーボードの下辺の円弧など) や、1 枚に並べた複数基板の内側の辺まで
    # くり抜きと取り違え、広い範囲を配線禁止にしてしまう (Ferris 0.1 で U1 の下が 71x10 mm 塞がっていた)
    # 外接矩形ではなく穴の形そのもの (基板端クリアランスぶん広げた多角形) を禁止にする。凹んだ穴の外接矩形は
    # 基板の広い範囲を覆う (Lily58 Lite Rev3 では面積 4181 mm² の穴の外接矩形が 12484 mm² あり、ファンアウトを塞いだ)
    edge = board.GetDesignSettings().m_CopperEdgeClearance
    holes = 0
    poly = pcbnew.SHAPE_POLY_SET()
    if board.GetBoardPolygonOutlines(poly):
        for i in range(poly.OutlineCount()):
            for j in range(poly.HoleCount(i)):
                holes += 1
                hp = pcbnew.SHAPE_POLY_SET()
                hp.AddOutline(poly.Hole(i, j))
                if edge > 0:
                    hp.Inflate(edge, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, FromMM(0.005))
                for k in range(hp.OutlineCount()):
                    o = hp.Outline(k)
                    bb = o.BBox()
                    pts = [(mm(o.CPoint(n).x), mm(o.CPoint(n).y)) for n in range(o.PointCount())]
                    for l in board.GetEnabledLayers().CuStack():
                        openings.append(dict(layer=board.GetLayerName(l), x0=mm(bb.GetLeft()), y0=mm(bb.GetTop()),
                                             x1=mm(bb.GetRight()), y1=mm(bb.GetBottom()), poly=pts))
    ok, dropped, npth = _export_dsn(board, zones, a['dsn'])
    return dict(dsn_ok=ok, pads=pads, tracks=tracks, vias=vias, zones=zlist, mask_openings=openings,
                dropped_rule_areas=dropped, cutouts=holes, mask_on_pads=mask_on_pads, npth_widened=npth,
                renamed_refs=sorted({r for _, r in renamed}), renamed_count=len(renamed),
                own_pad_vias=own_pad_vias,
                released=released, unrouted=unrouted,
                locked=sum(1 for t in tracks + vias if t['locked']),
                edge_clearance_um=round(mm(board.GetDesignSettings().m_CopperEdgeClearance) * 1000),
                min_track_um=round(mm(board.GetDesignSettings().m_TrackMinWidth) * 1000),
                hole_clearance_um=round(mm(board.GetDesignSettings().m_HoleClearance) * 1000),
                min_clearance_um=round(mm(board.GetDesignSettings().m_MinClearance) * 1000),
                min_via_um=round(mm(board.GetDesignSettings().m_ViasMinSize) * 1000),
                min_annular_um=round(mm(board.GetDesignSettings().m_ViasMinAnnularWidth) * 1000))


def add(a):
    """配線とビアをロック状態で足し、DSN を書き直す。足したものの UUID を返す (取り込み後にロックを外すため)。"""
    board, zones = _load(a['pcb'])
    code = {p.GetNetname(): p.GetNetCode() for fp in board.GetFootprints() for p in fp.Pads()}
    uuids = []
    for t in a['tracks']:
        tr = pcbnew.PCB_TRACK(board)
        tr.SetStart(pcbnew.VECTOR2I(FromMM(t['x1']), FromMM(t['y1'])))
        tr.SetEnd(pcbnew.VECTOR2I(FromMM(t['x2']), FromMM(t['y2'])))
        tr.SetWidth(FromMM(t['w']))
        tr.SetLayer(board.GetLayerID(t['layer']))
        tr.SetNetCode(code[t['net']])
        tr.SetLocked(True)
        board.Add(tr)
        uuids.append(tr.m_Uuid.AsString())
    for v in a['vias']:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(FromMM(v['x']), FromMM(v['y'])))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(FromMM(v['d']))
        via.SetDrill(FromMM(v['drill']))
        via.SetNetCode(code[v['net']])
        via.SetLocked(True)
        board.Add(via)
        uuids.append(via.m_Uuid.AsString())
    _refill(board, zones)
    board.Save(a['pcb'])
    _unique_refs(board)
    return dict(dsn_ok=_export_dsn(board, zones, a['dsn'])[0], uuids=uuids)


def import_ses(a):
    """SES を取り込む。KiCad はロック済みの配線を残し、それ以外を SES の内容で置き換える。"""
    board, zones = _load(a['pcb'])
    locked = sum(1 for t in board.GetTracks() if t.IsLocked())
    renamed = _unique_refs(board)       # DSN と同じ名前にしてから取り込み、保存の前に戻す
    ok = pcbnew.ImportSpecctraSES(board, a['ses'])
    for fp, r in renamed:
        fp.SetReference(r)
    locked_after = sum(1 for t in board.GetTracks() if t.IsLocked())
    widened = {}                        # UUID → 元の幅 (nm)。太らせて違反になった線を戻せるように
    min_w = FromMM(a.get('min_track_um', 0) / 1000.0)
    class_w = {n: FromMM(w / 1000.0) for n, w in (a.get('class_widths_um') or {}).items()}
    # Freerouting は混んだ場所で線幅をネットクラスの 75% や 50% に絞る。配線の途中にも入り、基板の
    # 最小線幅を下回ることもあるので、ネットクラスの幅 (少なくとも最小線幅) へ戻す
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T or t.IsLocked():
            continue
        want = max(min_w, class_w.get(t.GetNetname(), class_w.get('', 0)))
        if t.GetWidth() < want - 1000:
            widened[t.m_Uuid.AsString()] = t.GetWidth()
            t.SetWidth(want)
    unlock = set(a.get('unlock') or ())           # このツールが一時的にロックして足したもの
    for t in board.GetTracks():
        if t.m_Uuid.AsString() in unlock:
            t.SetLocked(False)
    shrunk = 0
    sizes = {FromMM(int(k) / 1000.0): FromMM(v / 1000.0) for k, v in (a.get('via_sizes') or {}).items()}
    for t in board.GetTracks():         # 穴クリアランスのために DSN 上で太らせたビアを、本来の寸法に戻す
        if t.Type() == pcbnew.PCB_VIA_T and not t.IsLocked():
            w = t.GetWidth(pcbnew.F_Cu)
            real = next((v for k, v in sizes.items() if abs(k - w) <= 1000), None)
            if real is not None:
                t.SetWidth(real)
                shrunk += 1
    _refill(board, zones)
    board.Save(a['pcb'])
    return dict(ses_ok=bool(ok), locked_before=locked, locked=locked_after, widened=widened, vias_resized=shrunk)


def remove(a):
    """指定した UUID の配線・ビアを消す。ロック済みのものは消さない。"""
    board, zones = _load(a['pcb'])
    want = set(a['uuids'])
    n = 0
    for t in list(board.GetTracks()):
        if t.m_Uuid.AsString() in want and not t.IsLocked():
            _remove(board, t)
            n += 1
    _refill(board, zones)
    board.Save(a['pcb'])
    return dict(removed=n)


def set_width(a):
    """a['widths']: {UUID: 幅 (nm)} のとおりに線幅を設定する。"""
    board, zones = _load(a['pcb'])
    n = 0
    for t in board.GetTracks():
        w = a['widths'].get(t.m_Uuid.AsString())
        if w is not None and t.Type() != pcbnew.PCB_VIA_T:
            t.SetWidth(int(w))
            n += 1
    _refill(board, zones)
    board.Save(a['pcb'])
    return dict(changed=n)


def stitch(a):
    """配線で分断されたベタの島を、同じネットの島が重なる場所にビアを打ってつなぐ。

    a['rules']: {ネット名: {'via_d', 'via_drill', 'clearance'}} (mm、キー '' は既定)。
    """
    board, zones = _load(a['pcb'])
    cu = list(board.GetEnabledLayers().CuStack())
    code = {p.GetNetname(): p.GetNetCode() for fp in board.GetFootprints() for p in fp.Pads()}
    bb = board.GetBoardEdgesBoundingBox()
    tracks = list(board.GetTracks())
    pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
    by_net = {}
    for z in zones:
        if not z.GetIsRuleArea() and z.GetNetname():
            by_net.setdefault(z.GetNetname(), []).append(z)
    added, report = 0, {}
    step = FromMM(a.get('step', 0.5))
    for net, zs in by_net.items():
        rule = a['rules'].get(net) or a['rules']['']
        r, clr = FromMM(rule['via_d'] / 2), FromMM(rule['clearance'])
        islands = []                    # (層, 塗りの多角形集合, 島の番号)
        for z in zs:
            for l in z.GetLayerSet().Seq():
                if l in cu and z.HasFilledPolysForLayer(l):
                    polys = z.GetFilledPolysList(l)
                    islands += [(l, polys, i) for i in range(polys.OutlineCount())]
        if len(islands) < 2:
            continue
        parent = list(range(len(islands)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def touching(pos, layers, reach):
            return [k for k, (l, polys, i) in enumerate(islands) if l in layers and polys.Contains(pos, i, reach)]

        def join(ks):
            for k in ks[1:]:
                parent[find(k)] = find(ks[0])

        # すでにある同じネットの銅が橋渡ししている島をまとめる
        for t in tracks:
            if t.GetNetname() != net:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                join(touching(t.GetPosition(), cu, t.GetWidth(pcbnew.F_Cu) // 2))
            else:
                join(touching(t.GetStart(), [t.GetLayer()], t.GetWidth() // 2)
                     + touching(t.GetEnd(), [t.GetLayer()], t.GetWidth() // 2))
        for p in pads:
            if p.GetNetname() == net:
                pb = p.GetBoundingBox()
                join(touching(p.GetPosition(), [l for l in cu if p.IsOnLayer(l)], max(pb.GetWidth(), pb.GetHeight()) // 2))
        before = len({find(k) for k in range(len(islands))})

        def inside_fully(pos, k):       # ビアの銅がすべて島の中に収まるか
            l, polys, i = islands[k]
            m = r + FromMM(0.05)
            return all(polys.Contains(pcbnew.VECTOR2I(pos.x + dx, pos.y + dy), i, 0)
                       for dx, dy in ((0, 0), (m, 0), (-m, 0), (0, m), (0, -m)))

        def clear_on(pos, layer):       # 島の外になる層で、ほかの銅に近すぎないか
            reach = r + clr
            for z in zones:
                if not z.GetIsRuleArea() and z.GetNetname() != net and z.GetLayerSet().Contains(layer) \
                        and z.HasFilledPolysForLayer(layer) and z.HitTestFilledArea(layer, pos, reach):
                    return False
            for t in tracks:
                if t.GetNetname() != net and t.IsOnLayer(layer) and t.HitTest(pos, reach):
                    return False
            return not any(p.GetNetname() != net and p.IsOnLayer(layer) and p.HitTest(pos, reach) for p in pads)

        def holes_clear(pos):           # 穴どうし、およびパッドとの間隔 (Freerouting と同じく同ネットでも離す)
            for t in tracks:
                if t.Type() == pcbnew.PCB_VIA_T and t.HitTest(pos, r + clr):
                    return False
            return not any(p.HitTest(pos, r + clr) for p in pads)

        y = bb.GetTop() + step
        while y < bb.GetBottom():
            x = bb.GetLeft() + step
            while x < bb.GetRight():
                pos = pcbnew.VECTOR2I(int(x), int(y))
                hit = [k for k in touching(pos, cu, 0) if inside_fully(pos, k)]
                if len({find(k) for k in hit}) >= 2:
                    layers_in = {islands[k][0] for k in hit}
                    if holes_clear(pos) and all(l in layers_in or clear_on(pos, l) for l in cu):
                        via = pcbnew.PCB_VIA(board)
                        via.SetPosition(pos)
                        via.SetViaType(pcbnew.VIATYPE_THROUGH)
                        via.SetWidth(FromMM(rule['via_d']))
                        via.SetDrill(FromMM(rule['via_drill']))
                        via.SetNetCode(code[net])
                        board.Add(via)
                        tracks.append(via)
                        join(hit)
                        added += 1
                x += step
            y += step
        report[net] = [before, len({find(k) for k in range(len(islands))})]
    if added:
        _refill(board, zones)
        board.Save(a['pcb'])
    return dict(added=added, nets=report)


if __name__ == '__main__':
    args = json.load(open(sys.argv[1], encoding='utf-8'))
    res = {'prepare': prepare, 'add': add, 'import': import_ses, 'remove': remove, 'stitch': stitch, 'set_width': set_width}[args['cmd']](args)
    json.dump(res, open(args['result'], 'w', encoding='utf-8'))
