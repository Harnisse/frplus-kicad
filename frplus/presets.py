"""基板メーカーの製造能力に合わせた下限 (プリセット)。設定の "preset" で選ぶ。

基板の設計ルールやネットクラスがこれより緩い (小さい) ときだけ、配線中はこの値まで引き上げる。
ユーザーの設定より厳しい値を緩めることはしない。元の基板の設計ルールも書き換えない。

JLCPCB の値は https://jlcpcb.com/capabilities/pcb-capabilities (2026-11-17 に確認) の標準の最小値。
銅箔は外層 1 oz、内層 0.5 oz (同社の既定) を想定する。2 oz にすると最小線幅・線間隔は広がる
(2 層 0.16 mm、多層 0.15 mm) ので、そのときはネットクラスを自分で広げること。
ページで追加料金の条件が書かれているのはビアだけ (穴 0.1/0.15 mm、または穴 0.2/0.25 mm でビア径 0.45 mm 未満は
割高)。線幅などはページに追加料金の記載が無いので、標準の最小値をそのまま使っている。
"""

PRESETS = {
    'jlcpcb-2layer': dict(
        layers=2,
        min_track=0.10,         # 2 層 1 oz の最小線幅・線間隔
        min_clearance=0.10,
        min_via_drill=0.20,     # 推奨最小のビア穴。これ未満は割高
        min_via_diameter=0.45,  # 穴 0.2/0.25 mm でビア径 0.45 mm 未満は割高
        min_via_annular=0.05,   # ビア径は穴径より 0.1 mm 以上大きく (推奨 0.15 mm)
        hole_clearance=0.20,    # ビアの穴と線 (Via hole to Track)
        edge_clearance=0.20,    # 基板外形 (ルーター加工) と銅。V カットなら 0.4 mm
        via_in_pad=False,       # 2 層の既定は穴埋めなし。パッド内のビアははんだを吸うので打たせない
        note='JLCPCB 2 層 (1 oz、追加料金なしの標準)',
    ),
    'jlcpcb-4layer': dict(
        layers=4,
        min_track=0.09,         # 多層 1 oz の最小線幅・線間隔
        min_clearance=0.09,
        min_via_drill=0.20,
        min_via_diameter=0.45,
        min_via_annular=0.05,
        hole_clearance=0.20,    # 内層のビア穴と銅も 0.2 mm
        edge_clearance=0.20,
        via_in_pad=False,       # 4 層も穴埋め (樹脂充填) は既定ではない
        note='JLCPCB 4 層 (外層 1 oz・内層 0.5 oz、追加料金なしの標準)',
    ),
    'jlcpcb-6layer': dict(
        layers=6,
        min_track=0.09,
        min_clearance=0.09,
        min_via_drill=0.20,
        min_via_diameter=0.45,
        min_via_annular=0.05,
        hole_clearance=0.20,
        edge_clearance=0.20,
        via_in_pad=None,        # 6 層以上は樹脂充填・キャップが既定の工程。パッド内ビアの扱いは設定 (auto) に任せる
        note='JLCPCB 6 層 (外層 1 oz・内層 0.5 oz、樹脂充填ビアが既定)',
    ),
}


def get(name):
    if name not in PRESETS:
        raise KeyError(name)
    return PRESETS[name]
