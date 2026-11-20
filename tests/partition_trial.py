"""同じ基板を領域に分けて並列に配線する試作 (実験用)。

  python tests/partition_trial.py <作業フォルダ (bench/<ラベル>/<基板>)> <jar> [timeout 秒]

作業フォルダにある r1_default.dsn (前処理済みの DSN) と kicad_prepare_result.json (パッド位置) を使う。
1. 片側だけで完結するネットが最も多く、またぐネットが最も少ない切れ目を x か y で選ぶ
2. 片側 A / B ごとに、他のネットを外し、反対側を配線禁止にした DSN を作って同時に配線する
3. A と B の結果を合わせて、全ネットでもう一度配線する (またぐネットと残り)
Freerouting 自身の未配線数と時間を表示する。比較対象は同じ作業フォルダの r1_default.log。
"""
import collections
import concurrent.futures
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from frplus import dsn as D            # noqa: E402
from frplus import tools as T          # noqa: E402
from frplus.sexpr import block_end, parse   # noqa: E402

MARGIN = 0.5    # mm。切れ目からこれ以内にパッドがあるネットは「またぐ」扱い


def choose_cut(pads):
    nets = collections.defaultdict(list)
    for p in pads:
        if p['net'] and not p['net'].startswith('unconnected-'):
            nets[p['net']].append(p)
    nets = {n: v for n, v in nets.items() if len(v) >= 2}
    best = None
    for axis in ('x', 'y'):
        vals = sorted(p[axis] for v in nets.values() for p in v)
        lo, hi = vals[0], vals[-1]
        for i in range(30, 71):
            cut = lo + (hi - lo) * i / 100
            a = {n for n, v in nets.items() if all(p[axis] < cut - MARGIN for p in v)}
            b = {n for n, v in nets.items() if all(p[axis] > cut + MARGIN for p in v)}
            ca = sum(len(nets[n]) - 1 for n in a)
            cb = sum(len(nets[n]) - 1 for n in b)
            score = (len(nets) - len(a) - len(b)) + abs(ca - cb) * 0.3
            if best is None or score < best[0]:
                best = (score, axis, cut, a, b)
    return best[1:]


def keep_nets(dsn, keep):
    """network の (net ...) と structure の (plane ...) を keep だけに絞る。

    外したネットの既存の配線・ビア (ベタへのビアなど) は、同じ場所の配線禁止領域に置き換えて障害物として残す。
    """
    dsn = _wiring_to_keepouts(dsn, keep)
    out, pos = [], 0
    for m in re.finditer(r'\((net|plane) ', dsn):
        if m.group(1) == 'net' and dsn.rfind('(network', 0, m.start()) < 0:
            continue                    # (net ...) は network の中のものだけ (wiring の (net X) は別物)
        e = block_end(dsn, m.start())
        node = parse(dsn[m.start():e])
        if m.group(1) == 'net' and not any(isinstance(x, list) and x[0] == 'pins' for x in node):
            continue                    # class の中などの (net ...) ではない
        if node[1] not in keep:
            out.append(dsn[pos:m.start()])
            pos = e
    return ''.join(out) + dsn[pos:]


def _wiring_to_keepouts(dsn, keep):
    signal = [n for n, t in re.findall(r'\(layer (\S+)\s*\(type (\w+)\)', dsn)]
    lines, drop = [], []
    for m in re.finditer(r'^\s*\((wire|via) .*\(net ("[^"]*"|\S+?)\).*$', dsn, re.M):
        net = m.group(2).strip('"')
        if net in keep:
            continue
        drop.append((m.start(), m.end()))
        node = parse(m.group(0).strip())
        if m.group(1) == 'via':
            d = re.search(r'_(\d+):\d+_um', node[1])
            r = (int(d.group(1)) if d else 600) / 2
            x, y = float(node[2]), float(node[3])
            for layer in signal:
                lines.append(_rect('keepout', layer, x - r, y - r, x + r, y + r))
        else:
            path = node[1]
            layer, w = path[1], float(path[2]) / 2
            c = [float(v) for v in path[3:]]
            xs, ys = c[0::2], c[1::2]
            lines.append(_rect('keepout', layer, min(xs) - w, min(ys) - w, max(xs) + w, max(ys) + w))
    for s, e in reversed(drop):
        dsn = dsn[:s] + dsn[e:]
    i = dsn.index('(boundary')
    e = block_end(dsn, i)
    return dsn[:e] + '\n' + '\n'.join(lines) + dsn[e:]


def _rect(kind, layer, x0, y0, x1, y1):
    return '    (%s "" (polygon %s 0  %s %s  %s %s  %s %s  %s %s  %s %s))' % (
        kind, layer, x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)


def region_keepouts(dsn, axis, cut, side):
    """反対側 (side='A' なら cut より大きい側) を全信号層で配線・ビア禁止にする。power 層は配線だけ禁止。"""
    xs = [float(v) for v in re.findall(r'\(boundary\s*\(path pcb 0\s+([^)]*)\)', dsn)[0].split()]
    bx, by = xs[0::2], xs[1::2]
    x0, x1, y0, y1 = min(bx), max(bx), min(by), max(by)
    c = cut * 1000
    if axis == 'x':
        r = (c, y0, x1, y1) if side == 'A' else (x0, y0, c, y1)
    else:                               # KiCad の y は DSN では負。KiCad で y < cut の側が A
        cy = -c
        r = (x0, y0, x1, cy) if side == 'A' else (x0, cy, x1, y1)
    layers = re.findall(r'\(layer (\S+)\s*\(type (\w+)\)', dsn)
    lines = []
    for name, typ in layers:
        kind = 'keepout' if typ == 'signal' else 'wire_keepout'
        lines.append('    (%s "" (polygon %s 0  %s %s  %s %s  %s %s  %s %s  %s %s))'
                     % (kind, name, r[0], r[1], r[2], r[1], r[2], r[3], r[0], r[3], r[0], r[1]))
    # 外したネットのベタが無くなった power 層は、全面を配線禁止にしないと信号が引かれる
    for name, typ in layers:
        if typ == 'power':
            lines.append('    (wire_keepout "" (polygon %s 0  %s %s  %s %s  %s %s  %s %s  %s %s))'
                         % (name, x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))
    i = dsn.index('(boundary')
    e = block_end(dsn, i)
    return dsn[:e] + '\n' + '\n'.join(lines) + dsn[e:]


def fr_unrouted(log):
    text = open(log, encoding='utf-8', errors='replace').read()
    m = re.findall(r'final score: [\d.]+ \((\d+) unrouted', text) or re.findall(r'score [\d.]+ \((\d+) unrouted', text)
    return int(m[-1]) if m else None


def main():
    work, jar = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 900
    out = os.path.join(work, 'partition')
    os.makedirs(out, exist_ok=True)
    base = open(os.path.join(work, 'r1_default.dsn'), encoding='utf-8').read()
    pads = json.load(open(os.path.join(work, 'kicad_prepare_result.json'), encoding='utf-8'))['pads']
    axis, cut, a, b = choose_cut(pads)
    print('切れ目: %s = %.2f mm、片側だけのネット A %d / B %d' % (axis, cut, len(a), len(b)))
    java = T.find_java(None)
    common = ['--router.optimizer.enabled=false']
    # 開発版は基板に残っているビアから via_in_pad を自動判定するので、元の実行と同じ値を明示する
    log1 = open(os.path.join(work, 'r1_default.log'), encoding='utf-8', errors='replace').read()
    for k in ('via_in_pad', 'via_touching_pad'):
        m = re.search(r'router\.autorouter\.%s = (\w+)' % k, log1)
        if m:
            common.append('--router.autorouter.%s=%s' % (k, m.group(1)))
    for k in re.findall(r'(--router\.(?:copper_to_edge_clearance_um|neck_width_um)=\d+)', ' '.join(open(os.path.join(work, 'r1_default.log'), encoding='utf-8', errors='replace').read().split())):
        common.append(k)
    jvm = ['-Xmx8192m', '-XX:ParallelGCThreads=6', '-XX:ConcGCThreads=3']

    def run(tag, text):
        dsn = os.path.join(out, tag + '.dsn')
        ses = os.path.join(out, tag + '.ses')
        open(dsn, 'w', encoding='utf-8', newline='\n').write(text)
        t0 = time.time()
        r = T.freerouting(jar, dsn, ses, out, tag, common, timeout, java, jvm)
        r['sec'] = round(time.time() - t0)
        return r

    parts = {'A': region_keepouts(keep_nets(base, a), axis, cut, 'A'),
             'B': region_keepouts(keep_nets(base, b), axis, cut, 'B')}
    reuse = '--reuse' in sys.argv          # 領域ごとの配線は前回の結果を使い、全体の段だけやり直す
    t0 = time.time()
    if reuse:
        res = {k: dict(ses_ok=True, unrouted=fr_unrouted(os.path.join(out, 'part_%s.log' % k)), sec=0, timed_out=False)
               for k in parts}
    else:
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            res = dict(zip(parts, ex.map(lambda k: run('part_' + k, parts[k]), parts)))
    t_par = round(time.time() - t0)
    for k, r in res.items():
        print('領域 %s: 未配線 %s、%d 秒%s' % (k, r['unrouted'], r['sec'], '、時間切れ' if r['timed_out'] else ''))
    merged = base
    for k in parts:
        if res[k]['ses_ok']:
            ses = os.path.join(out, 'part_%s.ses' % k)
            merged = D.merge_session(merged, open(ses, encoding='utf-8').read(), 'normal')[0]
    r = run('whole', merged)
    print('全体: 未配線 %s、%d 秒%s' % (r['unrouted'], r['sec'], '、時間切れ' if r['timed_out'] else ''))
    print('分割配線の合計: %d 秒 (並列部分 %d 秒)' % (t_par + r['sec'], t_par))
    print('比較 (分割なしの 1 回目 r1_default): 未配線 %s' % fr_unrouted(os.path.join(work, 'r1_default.log')))
    json.dump(dict(axis=axis, cut=cut, a=len(a), b=len(b), parts={k: {x: v[x] for x in ('unrouted', 'sec', 'timed_out')}
                                                                    for k, v in res.items()},
                   whole={x: r[x] for x in ('unrouted', 'sec', 'timed_out')}, total_sec=t_par + r['sec']),
              open(os.path.join(out, 'summary.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
