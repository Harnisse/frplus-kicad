"""外部プログラム (KiCad 付属 python、kicad-cli、Freerouting) の呼び出し。"""
import collections
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_JAVA = 25           # Freerouting v2.5 の jar は Java 25 用に作られている
SUPPORTED = ('2.5.0', '2.5.1')     # 設定名と挙動を実測で確かめたバージョン (2.5.1 は master 01db81c のビルド)


class ToolError(RuntimeError):
    pass


def find_kicad_bin(configured=None):
    cands = [configured] if configured else sorted(glob.glob(r'C:\Program Files\KiCad\*\bin'), reverse=True)
    for d in cands:
        if d and os.path.exists(os.path.join(d, 'python.exe')) and os.path.exists(os.path.join(d, 'kicad-cli.exe')):
            return d
    raise ToolError('KiCad の bin フォルダが見つかりません。設定の kicad_bin で指定してください。')


def find_jar(configured=None):
    cands = [configured, os.environ.get('FRPLUS_JAR')]
    base = os.path.join(os.path.dirname(HERE), 'freerouting')
    cands += sorted(glob.glob(os.path.join(base, 'dev', '*.jar')), reverse=True)      # 開発版のビルドがあれば優先
    cands += sorted(glob.glob(os.path.join(base, 'freerouting-*.jar')), reverse=True)
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise ToolError('Freerouting の jar が見つかりません。設定の freerouting_jar で指定してください。')


def java_version(exe):
    """java のメジャーバージョン (起動できなければ None)。"""
    try:
        p = subprocess.run([exe, '-version'], capture_output=True, text=True, errors='replace', timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)', p.stderr + p.stdout)
    return int(m.group(1)) if m else None


def find_java(configured=None):
    """Freerouting を動かす java。設定 → 環境変数 FRPLUS_JAVA → ツール同梱の jre → PATH の順に探す。"""
    exe = 'java.exe' if os.name == 'nt' else 'java'
    cands = [configured, os.environ.get('FRPLUS_JAVA'), os.path.join(os.path.dirname(HERE), 'jre', 'bin', exe), 'java']
    seen = []
    for c in cands:
        if not c or (c != 'java' and not os.path.exists(c)):
            continue
        v = java_version(c)
        if v and v >= MIN_JAVA:
            return c
        seen.append('%s (%s)' % (c, 'バージョン %d' % v if v else '起動できない'))
    raise ToolError('Java %d 以降が見つかりません。install.bat を実行するか、設定の java で java.exe を指定してください。%s'
                    % (MIN_JAVA, ' 見つかったもの: ' + ', '.join(seen) if seen else ''))


def total_memory_mb():
    """搭載メモリ (MB)。Windows・Linux・macOS で実際の値を読む。取れなければ 16 GB とみなす。

    Linux では /proc/meminfo の MemTotal を読み、cgroup (コンテナなど) のメモリ上限がそれより小さければそちらを使う。
    """
    if sys.platform == 'win32':
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = ([('len', ctypes.c_ulong), ('load', ctypes.c_ulong)]
                            + [(n, ctypes.c_ulonglong) for n in ('tp', 'ap', 'tf', 'af', 'tv', 'av', 'ave')])
            m = MS()
            m.len = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.tp // (1024 * 1024)
        except (AttributeError, OSError):
            pass
    elif sys.platform == 'darwin':
        try:
            return int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True,
                                      timeout=10).stdout.strip()) // (1024 * 1024)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    else:
        mb = _linux_memory_mb()
        if mb:
            return mb
    return 16384


def _linux_memory_mb(meminfo='/proc/meminfo',
                     cgroups=('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes')):
    """MemTotal (MB)。cgroup の上限がそれより小さければ上限を返す。読めなければ None。"""
    total = None
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    total = int(line.split()[1]) // 1024        # kB
                    break
    except (OSError, ValueError, IndexError):
        return None
    if total is None:
        return None
    for path in cgroups:
        try:
            with open(path) as f:
                v = f.read().strip()
            if v.isdigit() and int(v) // (1024 * 1024) < total:  # 上限なしは "max" か非常に大きい値
                total = int(v) // (1024 * 1024)
        except OSError:
            pass
    return total


def jar_has_setting(jar, name):
    """その設定名を jar が知っているか。知らない設定を渡すと警告だけで無視されるので、渡す前に確かめる。"""
    import zipfile
    try:
        with zipfile.ZipFile(jar) as z:
            return any(name.encode() in z.read(n) for n in z.namelist()
                       if n.startswith('app/freerouting/settings/') and n.endswith('Settings.class'))
    except (OSError, zipfile.BadZipFile):
        return False


def kicad(kicad_bin, workdir, **args):
    """kicad_side.py を KiCad 付属 python で実行し、結果の dict を返す。"""
    apath = os.path.join(workdir, 'kicad_%s_args.json' % args['cmd'])
    args['result'] = os.path.join(workdir, 'kicad_%s_result.json' % args['cmd'])
    if os.path.exists(args['result']):
        os.remove(args['result'])
    json.dump(args, open(apath, 'w', encoding='utf-8'))
    p = subprocess.run([os.path.join(kicad_bin, 'python.exe'), os.path.join(HERE, 'kicad_side.py'), apath],
                       capture_output=True, text=True, errors='replace')
    if not os.path.exists(args['result']):
        # SWIG の "memory leak" 行は無害だが大量に出て本当のエラーを隠すので落とす
        err = '\n'.join(l for l in (p.stderr + p.stdout).splitlines() if 'memory leak' not in l)
        raise ToolError('KiCad 側の処理 (%s) が失敗しました (終了コード %d):\n%s'
                        % (args['cmd'], p.returncode, err[-2000:]))
    return json.load(open(args['result'], encoding='utf-8'))


def drc(kicad_bin, pcb, out_json):
    """DRC を実行して集計する。戻り値: {'unconnected', 'types': {種別: 件数}, 'errors': {…}, 'dangling': [uuid]}"""
    subprocess.run([os.path.join(kicad_bin, 'kicad-cli.exe'), 'pcb', 'drc', '--format', 'json', '--severity-all',
                    '-o', out_json, pcb], capture_output=True)
    if not os.path.exists(out_json):
        raise ToolError('DRC を実行できませんでした: ' + pcb)
    d = json.load(open(out_json, encoding='utf-8'))
    v = d.get('violations', [])
    return dict(
        unconnected=len(d.get('unconnected_items', [])),
        unconnected_items=[[i['description'] for i in u['items']] for u in d.get('unconnected_items', [])],
        unconnected_pair_pos=[[(i['pos']['x'], i['pos']['y']) for i in u['items'] if 'pos' in i]
                              for u in d.get('unconnected_items', [])],
        unconnected_pos=[(i['pos']['x'], i['pos']['y']) for u in d.get('unconnected_items', []) for i in u['items']
                         if 'pos' in i],          # mm、KiCad の座標 (Y は下向き)
        # 項目の説明に "[ネット名]" が入っている
        unconnected_nets=sorted({m for u in d.get('unconnected_items', []) for i in u['items']
                                 for m in re.findall(r'\[(.+?)\]', i['description'])[:1]}),
        clearance_uuids=[i['uuid'] for x in v if x['type'] == 'clearance' for i in x['items']],
        types=dict(collections.Counter(x['type'] for x in v)),
        errors=dict(collections.Counter(x['type'] for x in v if x['severity'] == 'error')),
        dangling=[i['uuid'] for x in v if x['type'] in ('track_dangling', 'via_dangling') for i in x['items']])


def _kill_tree(proc):
    """子プロセスごと止める。Windows の java は中継プロセス経由で起動されることがあり、
    proc.kill() だけでは本体が残って動き続け、作業フォルダを掴んだままになる。"""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
    else:
        proc.kill()
    proc.wait()


def freerouting(jar, dsn, ses, workdir, tag, extra=(), timeout=None, java='java', jvm=()):
    """Freerouting をヘッドレスで 1 回実行する。"""
    ud = os.path.join(workdir, 'freerouting_userdata_' + tag)    # 並列実行で設定ファイルを取り合わないように
    os.makedirs(ud, exist_ok=True)
    log = os.path.join(workdir, tag + '.log')
    if os.path.exists(ses):
        os.remove(ses)
    cmd = [java] + list(jvm) + ['-jar', jar, '-de', dsn, '-do', ses, '-l', 'en', '--gui.enabled=false',
           '--usage_and_diagnostic_data.disable_analytics=true',      # 既定では利用統計を送信する
           '--user_data_path=' + ud] + list(extra)
    if timeout:
        # 時間切れでもそこまでの結果を保存させる。外から止めると何も残らない
        cmd.append('--router.job_timeout=%d' % timeout)
        timeout += 180                          # 保存する時間を見込んで、強制終了はその後
    with open(log, 'w', encoding='utf-8') as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=workdir)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
    text = open(log, encoding='utf-8', errors='replace').read()
    ver = re.search(r'Freerouting v([\d.]+)', text)
    # 存在しない設定名は警告だけで黙って無視される。気付かないと実験が無意味になるのでエラーにする
    bad = re.findall(r'(?:Unknown settings property|Failed to apply CLI router setting): *([^\n]+)', text)
    if bad:
        raise ToolError('Freerouting が設定を受け付けませんでした: ' + '; '.join(sorted(set(bad))))
    m = re.findall(r'Auto-routing stage completed.*?final score: ([\d.]+) \((\d+) unrouted', text)
    if not m:       # 時間切れだと集計行が出ないことがある。最後に終わったパスの数字を使う
        m = re.findall(r'Auto-routing pass #\d+.*?score ([\d.]+) \((\d+) unrouted', text)[-1:]
    failed = re.search(r'could not be routed[^\n]*\n((?:  .*\n)+)', text)
    return dict(tag=tag, version=ver.group(1) if ver else None, ses_ok=os.path.exists(ses),
                score=float(m[-1][0]) if m else None, unrouted=int(m[-1][1]) if m else None,
                optimized=bool(m) and 'Skipping optimization' not in text, timed_out='TIMED_OUT' in text,
                failed=failed.group(1).rstrip().split('\n') if failed else [], log=log)
