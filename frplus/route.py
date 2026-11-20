"""配線の一連の流れ: 作業用コピー → 前処理・事前診断 → Freerouting (一括 + 再開) → 取り込み → 掃除 → DRC → 反映。"""
import concurrent.futures
import json
import os
import re
import shutil
import time

from . import dsn as D
from . import geom as G
from . import presets as P
from . import tools as T

# Freerouting は決定的で乱数を持たないので、設定を変えた案を並べて良いものを選ぶ
VARIANTS = {
    'default': [],
    'nofanout': ['--router.fanout.enabled=false'],
    'noneck': ['--router.automatic_neckdown=false'],
    'plain': ['--router.fanout.enabled=false', '--router.automatic_neckdown=false'],
    # 以下は開発版 (v2.5.1 以降) にしか無い設定。v2.5.0 で指定するとエラーになる
    'seed1': ['--router.autorouter.item_order_seed=1'],
    'seed2': ['--router.autorouter.item_order_seed=2'],
    'seed3': ['--router.autorouter.item_order_seed=3'],
    'nofan_seed1': ['--router.fanout.enabled=false', '--router.autorouter.item_order_seed=1'],
    # コスト違い (Altium Situs の Completion パスの考え方: 残りを通すためにビアを安く、層の向きに逆らう線を高く)。
    # 既定はビア 50、引き剥がし開始 100、向きの重み 1.0 (向きの区別なし)
    'cheapvia': ['--router.scoring.via_costs=10'],
    'cheapvia_dir': ['--router.scoring.via_costs=10', '--router.scoring.default_undesired_direction_trace_cost=2.5'],
    'ripup_low': ['--router.scoring.start_ripup_costs=30'],
    'nofan_cheapvia': ['--router.fanout.enabled=false', '--router.scoring.via_costs=10'],
}

DEFAULTS = dict(
    java=None,              # java.exe の場所。省略時は同梱の jre、無ければ PATH の java
    freerouting_jar=None,   # 省略時は <このツール>/freerouting/freerouting-*.jar
    kicad_bin=None,         # 省略時は C:\Program Files\KiCad\*\bin
    release_nets=[],        # ロック済み配線を外して引き直させるネット (明示したものだけ)
    plane_layers=[],        # ベタ専用にする層 (例: ["In1.Cu", "In2.Cu"])
    plane_fanout=True,      # 内層ベタのネットの表面実装パッドにビアを足す
    on_trapped='stop',      # 出口の無いパッドを見つけたとき: "stop" = 配線せず終了、"warn" = 報告して続行
    unroute=False,          # True ならロックされていない既存配線を消してから配線する
    variants='auto',        # 並列に試す Freerouting の設定案。"auto" = 4 案、開発版のやり直しの回はコスト違いを足して 8 案
    max_resume=3,           # 未配線が残ったとき、結果を引き継いで再実行する最大回数
    resume_ripup=True,      # 再実行のとき、前回の配線を Freerouting が引き剥がしてよいことにする
    local_ripup_mm=3.0,     # 全体の引き剥がしで改善しないとき、未接続の箇所からこの距離までの配線だけ引き剥がせるようにする
    reroute_broken_pours=True,  # ベタ経由でつながらなかったネットを、普通のネットとして引き直す
    stitch_pours=True,      # 配線で分断されたベタの島を、ビアを足してつなぐ
    mask_keepouts=True,     # パッド以外のレジスト開口 (ロゴなど) や銅箔層の文字・図形の上に配線させない
    accept_errors=[],       # 配線で増えても反映を止めない DRC エラー種別 (例: ["starved_thermal"])
    max_parallel=None,      # 同時に動かす Freerouting の本数の上限。null = 搭載メモリから決める (1 本 2 GB、メモリの約 7 割まで)
    thermal_retry=4,        # 完走したのに残ったエラーが熱逃げ (starved_thermal) だけのとき、最良の結果を作った配線を
                            # 別の設定の案でこの本数までやり直し、エラーの無い結果を探す。0 でしない
    max_passes=100,         # Freerouting 1 回あたりの配線パス数の上限。時間で縛ると速いマシンほど結果が良くなるので、回数で縛る。
                            # 改善が 10 パス止まれば Freerouting 自身がその前に止める (開発版)。20 では 2 層の Lily58 でも足りなかった
    timeout_sec=3600,       # 止まらなくなったときの保険の時間上限 (秒)。時間切れでもそこまでの結果は使う。null で無制限
    via_in_pad='auto',      # 同じネットのパッドの中にビアを打たせるか。"auto" = 元の配線がそうしていれば許可 (開発版のみ)
    via_touching_pad='auto',  # 同じネットのパッドのすぐ隣にビアを打たせるか。"auto" = 同上
    optimize='complete',    # Freerouting の最適化段: "complete" = 完走してから 1 回だけ、"always" = 毎回、"never" = 使わない (開発版のみ)
    preset=None,            # 基板メーカーの製造能力に合わせた下限。"jlcpcb-2layer" "jlcpcb-4layer" "jlcpcb-6layer" (frplus/presets.py)
    extra_args=[],          # Freerouting にそのまま渡す追加の引数 (実験用)
)


def load_config(board, path=None):
    cfg = dict(DEFAULTS)
    path = path or os.path.splitext(board)[0] + '.frplus.json'
    if os.path.exists(path):
        user = json.load(open(path, encoding='utf-8'))
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise T.ToolError('設定ファイルに未知の項目があります: ' + ', '.join(sorted(unknown)))
        cfg.update(user)
    if cfg['optimize'] not in ('complete', 'always', 'never'):
        raise T.ToolError('optimize は "complete" "always" "never" のいずれかにしてください')
    if cfg['preset'] is not None and cfg['preset'] not in P.PRESETS:
        raise T.ToolError('preset に使えるのは %s です' % ', '.join(P.PRESETS))
    if cfg['on_trapped'] not in ('stop', 'warn'):
        raise T.ToolError('on_trapped は "stop" か "warn" にしてください')
    return cfg


def _new_errors(before, after):
    """配線前より増えたエラー種別 (シルクなど元からある違反を配線のせいにしない)。"""
    return {k: n - before.get(k, 0) for k, n in after.items() if n > before.get(k, 0)}


def _save(report):
    json.dump(report, open(os.path.join(report['workdir'], 'report.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def prepare(board, cfg, workdir, say):
    """作業用コピーを作って前処理と事前診断まで行う。戻り値: 以降の段で使う dict。"""
    board = os.path.abspath(board)
    stem = os.path.splitext(os.path.basename(board))[0]
    workdir = os.path.abspath(workdir or os.path.splitext(board)[0] + '_frplus')
    os.makedirs(workdir, exist_ok=True)
    kbin = T.find_kicad_bin(cfg['kicad_bin'])
    report = dict(board=board, workdir=workdir, started=time.strftime('%Y-%m-%d %H:%M:%S'), stages=[],
                  ok=False, applied=False)

    # 作業用コピー。元の基板には最後の反映まで触らない
    work = os.path.join(workdir, stem + '.kicad_pcb')
    shutil.copyfile(board, work)
    for ext in ('.kicad_pro', '.kicad_dru'):    # ネットクラスと DRC ルールはこちらにある
        if os.path.exists(os.path.splitext(board)[0] + ext):
            shutil.copyfile(os.path.splitext(board)[0] + ext, os.path.splitext(work)[0] + ext)
    report['result'] = work

    raw_dsn = os.path.join(workdir, 'board.dsn')
    geo = T.kicad(kbin, workdir, cmd='prepare', pcb=work, dsn=raw_dsn, release_nets=cfg['release_nets'],
                  plane_layers=cfg['plane_layers'], unroute=cfg['unroute'])
    if not geo['dsn_ok']:
        raise T.ToolError('DSN を書き出せませんでした。')
    if cfg['preset']:
        _apply_preset(geo, cfg['preset'], open(raw_dsn, encoding='utf-8').read(), say)
    say('前処理: ロック済み %d 個を固定配線として保持、固定解除 %d 個、既存配線の削除 %d 個'
        % (geo['locked'], geo['released'], geo['unrouted']))
    if geo['dropped_rule_areas']:
        say('前処理: 配線を禁止していないルールエリア %d 個を、配線禁止領域から除外' % geo['dropped_rule_areas'])
    if geo.get('mask_on_pads'):
        say('前処理: パッドの上に図形で描かれたレジスト開口 %d か所は、パッドのための開口として配線禁止にしません' % geo['mask_on_pads'])
    if geo.get('npth_widened'):
        say('前処理: 穴クリアランスを守るため、パッド径が穴径より大きい NPTH %d 個を DSN の中だけ広げました' % geo['npth_widened'])
    if geo.get('renamed_count'):
        say('前処理: 参照名が重複する部品 %d 個 (%s) を、DSN の中だけ別名にしました (基板の参照名は変えません)'
            % (geo['renamed_count'], '、'.join(geo['renamed_refs'][:5])))

    def load_dsn(quiet=False):
        text, notes = D.enforce_minimums(open(raw_dsn, encoding='utf-8').read(), geo['min_track_um'], geo['min_clearance_um'])
        if notes and not quiet:
            say('前処理: ネットクラスが基板の最小値を下回っているので引き上げます (%s)' % '、'.join(notes))
        return text

    text = load_dsn()
    model = G.Board(geo, D.net_rules(text), D.signal_layers(text), D.outline_boxes(text),
                    geo['edge_clearance_um'] / 1000)

    # 電源ピン → 内層ベタのビア。Freerouting は内層ベタへ落とすビアを自分では打てない
    added, failed = [], []
    if cfg['plane_fanout']:
        tracks, vias, failed = G.plane_fanout(model)
        if vias:
            res = T.kicad(kbin, workdir, cmd='add', pcb=work, dsn=raw_dsn, tracks=tracks, vias=vias)
            if not res['dsn_ok']:
                raise T.ToolError('DSN を書き出せませんでした。')
            added = res['uuids']
            text = load_dsn(quiet=True)
        say('前処理: 内層ベタへのビアを %d 個追加%s' % (len(vias), '、置けなかったパッド %d 個' % len(failed) if failed else ''))
        for f in failed:
            say('  ビアを置けない: ' + f)

    trapped = G.trapped_pads(model)
    for t in trapped:
        top = sorted(t['blockers'].items(), key=lambda kv: -kv[1])[:4]
        say('出口なし: %s [%s]  塞いでいるもの: %s' % (t['pad'], t['net'], '、'.join(k for k, _ in top)))
        if t['nets']:
            say('  → 設定の release_nets に %s のいずれかを入れると、そのロック済み配線を外して引き直します'
                % json.dumps(t['nets'], ensure_ascii=False))

    floor = max(r['clearance'] for r in model.rules.values())
    text, classes = D.add_pad_clearances(text, geo['pads'], floor)
    text, nth = D.add_pth_inner_shapes(text, geo['pads'], model.rule('')['clearance'], geo['hole_clearance_um'] / 1000)
    if nth:
        say('前処理: 内層の銅を持たないスルーホールのパッド形状 %d 種類に、内層の穴の形を追加' % nth)
    for cls, n in classes.items():
        say('前処理: 部品・パッドに個別に設定されたクリアランス %.2f mm をパッド %d 個に適用' % (int(cls[5:]) / 1000, n))
    if cfg['mask_keepouts'] and geo['mask_openings']:
        text = D.add_keepouts(text, geo['mask_openings'])
        say('前処理: レジスト開口や銅箔層の文字・図形、基板内側のくり抜き (%d 個) の周り、計 %d か所を配線禁止に設定'
            % (geo.get('cutouts', 0), len(geo['mask_openings'])))
    text, via_real, via_sizes = D.resize_vias(text, model.rule('')['clearance'], geo['hole_clearance_um'] / 1000,
                                              geo['min_via_um'] / 1000, geo['min_annular_um'] / 1000)
    for old, new in via_real.items():
        say('前処理: ビア外径 %.2f mm が基板の最小値を満たさないので %.2f mm に広げます' % (old / 1000, new / 1000))
    for big, real in via_sizes.items():
        say('前処理: 穴クリアランス %.2f mm を守るため、配線中はビア外径を %.2f → %.2f mm として扱います'
            % (geo['hole_clearance_um'] / 1000, real / 1000, big / 1000))
    base, nsnap = D.snap_to_pads(text, geo['pads'])
    say('前処理: パッド中心に届いていない端点を %d か所補正' % nsnap)
    report['prepare'] = dict(locked=geo['locked'], released=geo['released'], unrouted=geo['unrouted'],
                             snapped=nsnap, plane_vias=len(added) // 2, plane_via_failed=failed,
                             edge_clearance_um=geo['edge_clearance_um'])
    report['trapped'] = trapped
    shutil.copyfile(work, work + '.prepared')   # 配線結果を取り込む前の状態。取り込みをやり直すときに使う
    return dict(board=board, work=work, workdir=workdir, kbin=kbin, base=base, added=added,
                edge=geo['edge_clearance_um'], min_track=geo['min_track_um'], trapped=trapped, report=report,
                own_pad_vias=geo['own_pad_vias'], locked_tracks=[t for t in geo['tracks'] if t['locked']],
                rules=model.rules, via_sizes=via_sizes, zone_nets=sorted({z['net'] for z in geo['zones'] if not z['rule_area'] and z['net']}))


def _apply_preset(geo, name, dsn_text, say):
    """プリセットの下限を、基板の設計ルールの最小値に重ねる (緩いときだけ引き上げる)。"""
    p = P.get(name)
    say('プリセット: %s' % p['note'])
    floors = [('min_track_um', 'min_track', '最小線幅'), ('min_clearance_um', 'min_clearance', '最小クリアランス'),
              ('hole_clearance_um', 'hole_clearance', '穴クリアランス'), ('edge_clearance_um', 'edge_clearance', '基板端クリアランス'),
              ('min_via_um', 'min_via_diameter', '最小ビア径')]
    floors.append(('min_annular_um', 'min_via_annular', '最小アニュラーリング'))
    for key, pk, label in floors:
        want = round(p[pk] * 1000)
        if geo[key] < want:
            say('  %s %.3f → %.3f mm' % (label, geo[key] / 1000, want / 1000))
            geo[key] = want
    layers = len(re.findall(r'\(layer \S+\s*\(type (?:signal|power|mixed|jumper)\)', dsn_text))
    if layers and layers != p['layers']:
        say('  注意: 基板は %d 層、プリセットは %d 層用です' % (layers, p['layers']))
    small = sorted({r['via_drill'] for r in D.net_rules(dsn_text).values() if r['via_drill'] < p['min_via_drill'] - 1e-6})
    if small:
        say('  注意: ビアの穴径 %s mm がプリセットの最小 %.2f mm 未満です (割高になる)。ネットクラスのビアを変えてください'
            % (' / '.join('%.2f' % d for d in small), p['min_via_drill']))


def check(board, cfg, workdir=None, say=print):
    """事前診断だけ行う。Freerouting は起動しない。"""
    ctx = prepare(board, cfg, workdir, say)
    ctx['report']['ok'] = not ctx['trapped']
    if not ctx['trapped']:
        say('出口の無いパッドは見つかりませんでした')
    _save(ctx['report'])
    return ctx['report']


def run(board, cfg, workdir=None, apply=False, say=print):
    ctx = prepare(board, cfg, workdir, say)
    report, work, workdir, kbin = ctx['report'], ctx['work'], ctx['workdir'], ctx['kbin']
    if ctx['trapped'] and cfg['on_trapped'] == 'stop':
        say('出口の無いパッドがあるので配線を中止しました (続行するには設定で on_trapped を "warn" に)')
        _save(report)
        return report
    jar = T.find_jar(cfg['freerouting_jar'])
    java = T.find_java(cfg['java'])
    before = ctx['before'] = T.drc(kbin, work, os.path.join(workdir, 'drc_before.json'))
    extra = []
    if ctx['edge'] > 0:                         # KiCad の DSN は基板端クリアランスを持たない
        extra.append('--router.copper_to_edge_clearance_um=%d' % ctx['edge'])
    if ctx['min_track'] > 0:                    # パッド手前で線を絞るとき、基板の最小線幅より細くさせない
        extra.append('--router.neck_width_um=%d' % ctx['min_track'])

    own = ctx['own_pad_vias']
    in_pad = own['in_pad'] > 0 if cfg['via_in_pad'] == 'auto' else bool(cfg['via_in_pad'])
    touching = (in_pad or own['near_pad'] > 0) if cfg['via_touching_pad'] == 'auto' else bool(cfg['via_touching_pad'])
    if cfg['preset'] and P.get(cfg['preset'])['via_in_pad'] is False:
        # 穴埋めが既定でない層数では、パッドの中や縁にかかるビアははんだを吸う。auto のときは禁止にする
        if cfg['via_in_pad'] == 'auto':
            in_pad = False
        if cfg['via_touching_pad'] == 'auto':
            touching = in_pad
    if own['in_pad'] or own['near_pad']:
        say('元の配線のビア %d 個のうち、同じネットのパッドの中が %d 個、すぐ隣が %d 個' % (own['total'], own['in_pad'], own['near_pad']))
    if T.jar_has_setting(jar, 'via_touching_pad'):
        # 開発版は「基板に残っているビア」から自動判定するが、配線を消すと判定材料が無くなるので明示する
        extra += ['--router.autorouter.via_in_pad=%s' % str(in_pad).lower(),
                  '--router.autorouter.via_touching_pad=%s' % str(touching).lower()]
        say('パッド内のビア: %s、パッド隣のビア: %s' % ('許可' if in_pad else '禁止', '許可' if touching else '禁止'))
    elif in_pad or touching:
        say('注意: この Freerouting はパッドの中や隣にビアを打てません (開発版が必要)。完走しにくくなります')
    report['via_in_pad'], report['via_touching_pad'] = in_pad, touching
    # 開発版の最適化段は未配線が残っていても走り、大きい基板では配線本体より長くかかって何も改善しない
    # (OpenDrone で 37 分、変化なし)。配線中は止めておき、完走したあとで 1 回だけ走らせる
    opt_mode = cfg['optimize'] if T.jar_has_setting(jar, 'improvement_threshold') else 'always'
    opt_off = ['--router.optimizer.enabled=false'] if opt_mode != 'always' else []
    if cfg['max_passes']:
        extra.append('--router.max_passes=%d' % cfg['max_passes'])
    extra += cfg['extra_args']
    variants = first = cfg['variants']
    if variants == 'auto':
        # 決め手になるやり直しの回は、コスト違いの案を足して本数を増やす (乱数シード違いは効果が誤差の範囲だった)。
        # 1 回目で完走する基板が多いので、1 回目は 4 案に留める
        first = ['default', 'nofanout', 'noneck', 'plain']
        # 前の回に通らなかったネットを優先クラスにして先に引かせる案も試したが、OpenDrone では最下位だった
        variants = first + (['cheapvia', 'cheapvia_dir', 'ripup_low', 'nofan_cheapvia']
                            if T.jar_has_setting(jar, 'default_undesired_direction_trace_cost') else [])
    unknown = set(variants) - set(VARIANTS)
    if unknown or not variants:
        raise T.ToolError('variants に使えるのは %s です' % ', '.join(VARIANTS))
    # 並べる本数に合わせて Java のメモリと GC スレッドを絞る (既定だと 1 本が搭載メモリの 1/4 と全コアを取りに行く)。
    # 最適化段を止めた配線本体は 1 スレッドで、OpenDrone でもヒープは 3 GB 以下だった
    def parallel_cap():
        """同時に動かす Freerouting の本数の上限。1 本あたりヒープ 2 GB として、搭載メモリの約 7 割まで。
        メモリの少ないマシン (7.9 GB の Linux) で 4〜8 本を同時に動かすと、例外も残さず強制終了される"""
        if cfg['max_parallel']:
            return max(1, int(cfg['max_parallel']))
        return max(1, int(T.total_memory_mb() * 0.7 // 2048))

    def jvm_for(k):
        heap = max(2048, min(8192, int(T.total_memory_mb() * 0.6 / k)))
        gc = max(2, (os.cpu_count() or 8) // k)
        return ['-Xmx%dm' % heap, '-XX:ParallelGCThreads=%d' % gc, '-XX:ConcGCThreads=%d' % max(1, gc // 2)]

    def freeroute(dsn_text, tag, variant, opts=opt_off, jvm=(), more=()):
        dsn_k = os.path.join(workdir, tag + '.dsn')
        ses_k = os.path.join(workdir, tag + '.ses')
        open(dsn_k, 'w', encoding='utf-8', newline='\n').write(dsn_text)
        t0 = time.time()
        r = T.freerouting(jar, dsn_k, ses_k, workdir, tag, opts + extra + VARIANTS[variant] + list(more), cfg['timeout_sec'], java, jvm)
        r.update(sec=round(time.time() - t0), ses=ses_k, variant=variant)
        return r

    def round_(dsn_text, n, runs=None, suffix=''):
        """設定違いの案を並列に配線し、それぞれ取り込んで KiCad の DRC で採点する。良い順に返す。"""
        if runs is None:
            vs = first if n == 1 else variants
            k = min(len(vs), parallel_cap())       # 入りきらない案は順番待ち (結果は同じで、時間だけ延びる)
            with concurrent.futures.ThreadPoolExecutor(k) as ex:
                # 前回の配線を全部固定した回は、2 パス目以降まったく良くならない (OpenDrone などで 5 本とも 20 パス同じ値)
                more = ['--router.max_passes=3'] if suffix == '_fix' else []
                runs = list(ex.map(lambda v: freeroute(dsn_text, 'r%d_%s%s' % (n, v, suffix), v, jvm=jvm_for(k),
                                                       more=more), vs))
        out = []
        for r in runs:
            report['stages'].append(r)
            if r['version'] and r['version'] not in T.SUPPORTED and not report.get('warned'):
                report['warned'] = True
                say('注意: Freerouting v%s は未検証です (検証済みは v%s)' % (r['version'], ', '.join(T.SUPPORTED)))
            if not r['ses_ok']:
                say('  案 %s: Freerouting が結果を出力しませんでした (ログ: %s)' % (r['variant'], r['log']))
                continue
            after, info = _finish(ctx, cfg, r['ses'])
            errs = _new_errors(before['errors'], after['errors'])
            errs.pop('unconnected_items', None)
            r.update(kicad_unconnected=after['unconnected'], kicad_new_errors=errs)
            say('  案 %-9s: 未接続 %d 件、増えたエラー %d 件 (Freerouting 上の未配線 %s、%d 秒%s)'
                % (r['variant'], after['unconnected'], sum(errs.values()), r['unrouted'], r['sec'],
                   '、時間切れ' if r['timed_out'] else ''))
            if r['timed_out'] and not report.get('warned_timeout'):
                report['warned_timeout'] = True
                say('  注意: 時間の上限 (timeout_sec) で打ち切った。結果がマシンの速さに左右されるので、'
                    'timeout_sec を増やすか max_passes を減らすとよい')
            shutil.copyfile(work, work + '.cand_' + r['tag'])
            out.append(((after['unconnected'], sum(errs.values())), r, after, info))
        out.sort(key=lambda c: c[0])
        return out

    # KiCad の DRC を正として、未接続が減らなくなるまで繰り返す
    # やり直しの回で前回の配線をどう渡すか。全体を引き剥がしてよい → 未接続の周りだけ → 全部固定、の順に試し、
    # 改善した方式を次の回の最初に使う。残りが少なくなると全体の引き剥がしは毎回悪くする (OpenDrone で 4 → 28 件)、
    # 全部固定では同じ所で止まる (3 → 3 件)
    modes = ['normal', 'local', 'route'] if cfg['resume_ripup'] else ['route']
    label = {'normal': ('', '前回の配線を引き剥がせる形で'), 'local': ('_loc', '未接続の周り %.1f mm だけ引き剥がせる形で' % cfg['local_ripup_mm']),
             'route': ('_fix', '前回の配線を固定して')}
    base, best, mode_i, nxt = ctx['base'], None, 0, None
    round_src = best_src = base     # その回の DSN の元 (ベタ定義を外したネットがあれば外した版)
    key = lambda c: c[0]
    for n in range(1, cfg['max_resume'] + 2):
        nv = len(first if n == 1 else variants)
        say('配線 %d 回目 (%d 案を並列実行%s)' % (n, nv, '、メモリに合わせて同時に %d 本まで' % parallel_cap() if parallel_cap() < nv else ''))
        if best is None:
            cands = round_(base, n)
        else:
            cands = []
            for i in range(mode_i, len(modes)):
                if i > mode_i:
                    say('配線 %d 回目: 改善しなかったので、%sもう一度配線' % (n, label[modes[i]][1]))
                cands = sorted(cands + round_(nxt(modes[i]), n, suffix=label[modes[i]][0]), key=key)
                if cands and cands[0][0] < best[0]:
                    mode_i = i
                    break
        if not cands:
            break
        if best is not None and cands[0][0] >= best[0]:
            say('配線 %d 回目: 改善しなかったので終了' % n)
            break
        best, best_src = cands[0], round_src
        after = best[2]
        if after['unconnected'] == 0:
            break
        # ベタ経由でつながるはずだったのに切れているネットは、ベタ定義を外して普通のネットとして引き直させる
        nets = [x for x in after['unconnected_nets'] if x in ctx['zone_nets']] if cfg['reroute_broken_pours'] else []
        src, count = D.strip_planes(base, nets)
        round_src = src
        if count:
            say('  ベタでつながらなかったネット %s は、次回は通常のネットとして引き直します' % json.dumps(nets, ensure_ascii=False))
        # (type route) で渡すと Freerouting は固定配線として扱い、前回の配線を引き剥がせない。(type normal) なら引き剥がせる
        ses_text = open(best[1]['ses'], encoding='utf-8').read()
        free = ([(x * 1000, -y * 1000) for x, y in after['unconnected_pos']], cfg['local_ripup_mm'] * 1000)
        nxt = (lambda mode, src=src, ses_text=ses_text, free=free:
               D.merge_session(src, ses_text, 'route' if mode == 'local' else mode, free if mode == 'local' else None)[0])
    if best is None:
        raise T.ToolError('Freerouting の実行に失敗しました。ログを確認してください: ' + workdir)
    def optimize(cand, src_base, tag):
        """完走した結果を最適化段にかける。悪くならなければ最適化後を、悪くなれば元を返す。"""
        v = cand[1]['variant']
        # (type route) は Freerouting では固定扱いになり最適化できないので、normal で渡す
        # 最良の結果を作った回と同じ元の DSN に重ねる。ベタ定義を外して引き直したネット (GND など) を
        # 元の DSN に戻すと、Freerouting はベタでつながるとみなして最適化でその線を消し、未接続が再発する
        src = D.merge_session(src_base, open(cand[1]['ses'], encoding='utf-8').read(), 'normal')[0]
        cands = round_(src, 0, [freeroute(src, tag, v, ['--router.optimizer.enabled=true'], jvm_for(1))])
        if cands and cands[0][0] <= cand[0]:
            return cands[0]
        say('最適化後の検証結果が悪くなったので、最適化前の結果を使います')
        return cand

    pre_opt = best
    if opt_mode == 'complete' and best[0][0] == 0:
        say('完走したので最適化段を実行')
        best = optimize(best, best_src, 'opt_' + best[1]['variant'])
    # 熱逃げのエラーは Freerouting がベタのスポークを考えずに線を通すことで起き、どこに線を通すかは案ごとに変わる。
    # 完走していて残りがそれだけなら、最良の結果を作った回と同じ入力 (最適化の前) を別の案で配線し直し、
    # 良くなればそれを最適化する。最適化の入力 (配線が載ったもの) から最適化つきでやり直すと、線をほとんど
    # 作り直せず未接続が出るうえ遅い (メモリ 7.9 GB の Linux マシンの Lily58 で 3 案とも未接続 1、1 本 10 分前後)
    errs = {k: v for k, v in best[1].get('kicad_new_errors', {}).items() if k not in cfg['accept_errors']}
    if cfg['thermal_retry'] and best[0][0] == 0 and errs and set(errs) == {'starved_thermal'}:
        has_cost = T.jar_has_setting(jar, 'default_undesired_direction_trace_cost')
        order = ['default', 'noneck', 'nofanout', 'plain', 'cheapvia', 'cheapvia_dir', 'ripup_low', 'nofan_cheapvia']
        tried = pre_opt[1]['variant']
        vs = [v for v in order if v != tried and (has_cost or v not in ('cheapvia', 'cheapvia_dir', 'ripup_low', 'nofan_cheapvia'))]
        vs = vs[:int(cfg['thermal_retry'])]
        if vs:
            tag = pre_opt[1]['tag']
            src = open(os.path.join(workdir, tag + '.dsn'), encoding='utf-8').read()
            more = ['--router.max_passes=3'] if tag.endswith('_fix') else []
            say('熱逃げのエラー (starved_thermal %d 件) だけが残ったので、%s と同じ入力を %d 案で配線し直し'
                % (errs['starved_thermal'], tag, len(vs)))
            k = min(len(vs), parallel_cap())
            with concurrent.futures.ThreadPoolExecutor(k) as ex:
                runs = list(ex.map(lambda v: freeroute(src, '%s_th_%s' % (tag, v), v, opt_off, jvm_for(k), more), vs))
            cands = [c for c in round_(src, 0, runs) if c[0][0] == 0 and c[0] < best[0]]
            if cands:
                cand = cands[0]
                if opt_mode == 'complete':
                    say('  %s で減ったので最適化段を実行' % cand[1]['tag'])
                    cand = optimize(cand, best_src, 'opt_' + cand[1]['variant'] + '_th')
                if cand[0] < best[0]:
                    best = cand
                else:
                    say('  最適化の後は減らなかったので、元の結果を使います')
            else:
                say('  やり直しでも減らなかったので、元の結果を使います')
    _, chosen, after, info = best
    shutil.copyfile(work + '.cand_' + chosen['tag'], work)
    T.drc(kbin, work, os.path.join(workdir, 'drc_routed.json'))
    report['best_stage'] = chosen['tag']
    if info['widened'] or info['narrow_left']:
        say('仕上げ: Freerouting が絞った線 %d 本をネットクラスの線幅に戻しました%s'
            % (info['widened'], '、%d 本は場所が無く細いまま' % info['narrow_left'] if info['narrow_left'] else ''))
    if info['stitch_vias']:
        say('仕上げ: ベタの島をつなぐビアを %d 個追加' % info['stitch_vias'])
    if info['removed']:
        say('仕上げ: 宙ぶらりんの線・ビアを %d 個削除' % info['removed'])
    removed = info['removed']

    new_err = _new_errors(before['errors'], after['errors'])
    new_err.pop('unconnected_items', None)
    accepted = {k: new_err.pop(k) for k in list(new_err) if k in cfg['accept_errors']}
    ok = after['unconnected'] == 0 and not new_err
    report.update(removed_dangling=removed, unconnected=after['unconnected'], accepted_errors=accepted, finish=info,
                  unconnected_items=after['unconnected_items'], new_errors=new_err,
                  dangling_left=len(after['dangling']), drc_types=after['types'], ok=ok)
    say('検証: 未接続 %d 件、配線で増えたエラー %s、残った宙ぶらりん %d 個 (ロック済み)'
        % (after['unconnected'], new_err or 'なし', len(after['dangling'])))
    if accepted:
        say('  設定により許容したエラー: %s' % accepted)
    for u in after['unconnected_items']:
        say('  未接続: ' + ' ⇔ '.join(u))
    dead = _locked_dead_ends(ctx, after)
    if dead:
        report['locked_dead_ends'] = dead
        say('  ロック済みの配線の先が未接続のまま残ったネット: %s' % '、'.join(dead))
        say('  → 設定の release_nets に入れると、そのロックを外して引き直します (混み合った場所では、それでもつながらないことがある): %s'
            % json.dumps(dead, ensure_ascii=False))
    _save(report)
    if apply:
        apply_result(workdir, say)
        report = json.load(open(os.path.join(workdir, 'report.json'), encoding='utf-8'))
    say('結果の基板: ' + work)
    return report


def _locked_dead_ends(ctx, after, reach=0.2):
    """未接続の箇所 (mm) にロック済みの線の端があるネット。人手で途中まで引いた線の先がつながっていない"""
    ends = [(t['net'], x, y) for t in ctx['locked_tracks'] for x, y in ((t['x1'], t['y1']), (t['x2'], t['y2']))]
    out = set()
    for item, pos in zip(after['unconnected_items'], after['unconnected_pair_pos']):
        nets = {n for d in item for n in re.findall(r'\[(.+?)\]', d)[:1]}
        for net, x, y in ends:
            if net in nets and any((x - px) ** 2 + (y - py) ** 2 <= reach ** 2 for px, py in pos):
                out.add(net)
    return sorted(out)


def _finish(ctx, cfg, ses):
    """配線前の基板に SES を取り込み、線幅の補正・ベタの縫い合わせ・掃除をして DRC する。戻り値: (DRC 結果, 内訳)"""
    work, workdir, kbin = ctx['work'], ctx['workdir'], ctx['kbin']
    out = os.path.join(workdir, 'drc_routed.json')
    shutil.copyfile(work + '.prepared', work)
    imp = T.kicad(kbin, workdir, cmd='import', pcb=work, ses=ses, unlock=ctx['added'], min_track_um=ctx['min_track'],
                  via_sizes=ctx['via_sizes'],
                  class_widths_um={n: round(r['width'] * 1000) for n, r in ctx['rules'].items()})
    if not imp['ses_ok'] or imp['locked'] != imp['locked_before']:
        raise T.ToolError('SES の取り込みに失敗しました (ロック済み配線 %d → %d)' % (imp['locked_before'], imp['locked']))
    after = T.drc(kbin, work, out)
    info = dict(widened=len(imp['widened']), narrow_left=0, stitch_vias=0, removed=0)

    # Freerouting が絞った線を最小線幅まで太らせた結果、クリアランス違反になったものは元の幅に戻す
    back = {u: imp['widened'][u] for u in after['clearance_uuids'] if u in imp['widened']}
    if back:
        T.kicad(kbin, workdir, cmd='set_width', pcb=work, widths=back)
        after = T.drc(kbin, work, out)
        info.update(widened=len(imp['widened']) - len(back), narrow_left=len(back))

    # 配線で分断されたベタの島をビアでつなぐ。悪化したら元に戻す
    if cfg['stitch_pours'] and after['unconnected'] and ctx['zone_nets']:
        keep = work + '.before_stitch'
        shutil.copyfile(work, keep)
        rules = {n: ctx['rules'].get(n) or ctx['rules'][''] for n in ctx['zone_nets'] + ['']}
        st = T.kicad(kbin, workdir, cmd='stitch', pcb=work, rules=rules)
        if st['added']:
            again = T.drc(kbin, work, out)
            worse = _new_errors(after['errors'], again['errors'])
            worse.pop('unconnected_items', None)
            if again['unconnected'] >= after['unconnected'] or worse:
                shutil.copyfile(keep, work)
            else:
                after = again
                info['stitch_vias'] = st['added']

    # 宙ぶらりんの線とビアを消す。Freerouting の最適化段は未配線が 1 本でも残ると走らないため
    for _ in range(8):
        if not after['dangling']:
            break
        keep = work + '.before_cleanup'
        shutil.copyfile(work, keep)
        n = T.kicad(kbin, workdir, cmd='remove', pcb=work, uuids=after['dangling'])['removed']
        if n == 0:                              # 残りはロック済み (ガード配線など意図的なもの)
            break
        again = T.drc(kbin, work, out)
        if again['unconnected'] > after['unconnected']:    # 接続を壊したら元に戻してやめる
            shutil.copyfile(keep, work)
            after = T.drc(kbin, work, out)
            break
        info['removed'] += n
        after = again
    return after, info


def apply_result(workdir, say=print):
    """検証を通った結果を元の基板に反映する。バックアップを取ってから上書きする。"""
    path = os.path.join(os.path.abspath(workdir), 'report.json')
    if not os.path.exists(path):
        raise T.ToolError('report.json がありません。先に route を実行してください: ' + workdir)
    report = json.load(open(path, encoding='utf-8'))
    if not report.get('ok') or 'unconnected' not in report:
        say('反映: 検証を通っていないので元の基板は変更していません')
        return report
    if os.path.getmtime(report['board']) > os.path.getmtime(report['result']):
        raise T.ToolError('配線を実行した後に元の基板が変更されています。route をやり直してください。')
    bak = report['board'] + time.strftime('.%Y%m%d-%H%M%S.bak')
    shutil.copyfile(report['board'], bak)
    shutil.copyfile(report['result'], report['board'])
    os.utime(report['result'])                  # 続けてもう一度 apply しても「変更されています」にならないように
    report.update(applied=True, backup=bak)
    _save(report)
    say('反映: 元の基板を更新しました (バックアップ: %s)' % bak)
    return report
