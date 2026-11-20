"""複数の基板を同じ条件で配線し、結果を 1 つの表にまとめる (標準 python で実行、無人で回せる)。

  python tests/bench.py <ラベル> <jar> [--variants a,b,c] [--only 名前,名前] [--set key=json ...]

結果は bench/<ラベル>/<基板名>/ と bench/<ラベル>/summary.json に残る。途中で止まっても、
終わった基板の分は summary.json に書かれている。
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARDS = [      # (名前, 基板, 元の設定ファイル)
    # 作者の非公開の 4 層基板 (事前配線あり / なし)。手元に無い基板は --only / --skip で外す
    ('board4l_pre', 'tests/fixture/board4l.kicad_pcb', 'tests/fixture/board4l.frplus.json'),
    ('board4l_bare', 'tests/fixture_bare/board4l.kicad_pcb', 'tests/fixture_bare/board4l.frplus.json'),
    ('pic_programmer', 'boards/pic_programmer_work/pic_programmer.kicad_pcb', 'boards/pic_programmer_work/pic_programmer.frplus.json'),
    ('multichannel', 'boards/multichannel_mixer_work/multichannel_mixer.kicad_pcb', 'boards/multichannel_mixer_work/multichannel_mixer.frplus.json'),
    ('stickhub', 'boards/sh_work/StickHub.kicad_pcb', 'boards/sh_work/StickHub.frplus.json'),
    ('opendrone', 'boards/od_work/OpenDrone_AIO_FC_F405_HW.kicad_pcb', 'boards/od_work/OpenDrone_AIO_FC_F405_HW.frplus.json'),
    # 人手で一部 (電源・USB、多ピン部品のファンアウト) を引いてから自動配線する使い方の再現 (tests/make_partial.py)
    ('stickhub_partial', 'boards/sh_partial/StickHub.kicad_pcb', 'boards/sh_partial/StickHub.frplus.json'),
    ('opendrone_partial', 'boards/od_partial/OpenDrone_AIO_FC_F405_HW.kicad_pcb', 'boards/od_partial/OpenDrone_AIO_FC_F405_HW.frplus.json'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('label')
    ap.add_argument('jar')
    ap.add_argument('--variants')
    ap.add_argument('--only')
    ap.add_argument('--skip')
    ap.add_argument('--set', action='append', default=[])
    a = ap.parse_args()
    out = os.path.join(ROOT, 'bench', a.label)
    os.makedirs(out, exist_ok=True)
    summary_path = os.path.join(out, 'summary.json')
    summary = json.load(open(summary_path, encoding='utf-8')) if os.path.exists(summary_path) else {}
    only = set(a.only.split(',')) if a.only else None
    skip = set(a.skip.split(',')) if a.skip else set()
    for name, board, cfg_path in BOARDS:
        if (only and name not in only) or name in skip:
            continue
        cfg = json.load(open(os.path.join(ROOT, cfg_path), encoding='utf-8'))
        cfg['freerouting_jar'] = os.path.abspath(a.jar)
        if a.variants:
            cfg['variants'] = a.variants.split(',')
        for kv in a.set:
            k, v = kv.split('=', 1)
            cfg[k] = json.loads(v)
        work = os.path.join(out, name)
        os.makedirs(work, exist_ok=True)
        cpath = os.path.join(out, name + '.config.json')
        json.dump(cfg, open(cpath, 'w', encoding='utf-8'), indent=1)
        t0 = time.time()
        with open(os.path.join(out, name + '.log'), 'w', encoding='utf-8') as f:
            rc = subprocess.run([sys.executable, '-m', 'frplus', 'route', os.path.join(ROOT, board), '--config', cpath,
                                 '--workdir', work], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONIOENCODING='utf-8')).returncode
        row = dict(exit=rc, sec=round(time.time() - t0))
        rpath = os.path.join(work, 'report.json')
        if os.path.exists(rpath):
            r = json.load(open(rpath, encoding='utf-8'))
            row.update(unconnected=r.get('unconnected'), new_errors=r.get('new_errors'), best=r.get('best_stage'),
                       rounds=max([int(s['tag'][1:].split('_')[0]) for s in r.get('stages', []) if s['tag'][0] == 'r'] or [0]),
                       finish=r.get('finish'), trapped=len(r.get('trapped') or []))
        summary[name] = row
        json.dump(summary, open(summary_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(a.label, name, json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
