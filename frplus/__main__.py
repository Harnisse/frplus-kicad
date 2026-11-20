"""python -m frplus {route | check | apply} ..."""
import argparse
import os
import sys

from . import __version__, route, tools


def _documents():
    """「ドキュメント」フォルダ。OneDrive などへ移してある場合もあるので Windows に尋ねる。"""
    if os.name == 'nt':
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
            return buf.value
    return os.path.join(os.path.expanduser('~'), 'Documents')


def doctor():
    """必要なものが揃っているかを表示する。揃っていれば 0。"""
    ok = True
    for name, find in (('KiCad', tools.find_kicad_bin), ('Freerouting', tools.find_jar), ('Java', tools.find_java)):
        try:
            print('%-12s %s' % (name, find()))
        except tools.ToolError as e:
            ok = False
            print('%-12s 見つかりません: %s' % (name, e))
    return 0 if ok else 2


def install_plugin(dest=None):
    """KiCad のプラグインフォルダに、このツールを読み込む数行のファイルを書く。"""
    tool = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not dest:
        ver = os.path.basename(os.path.dirname(tools.find_kicad_bin()))        # KiCad/9.0/bin → 9.0
        # KiCad をまだ一度も起動していない環境ではフォルダが無いので作る
        dest = os.path.join(_documents(), 'KiCad', ver, 'scripting', 'plugins')
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, 'frplus_plugin.py')
    lines = ['# frplus の読み込み用。`python -m frplus install-plugin` が書いたファイル。',
             'import sys', '', 'sys.path.insert(0, %r)' % tool,
             'from frplus.kicad_plugin import register  # noqa: E402', '', 'register()', '']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog='frplus', description='Freerouting の入力を整え、結果を検証してから基板に反映する')
    ap.add_argument('--version', action='version', version='frplus ' + __version__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name, text in (('route', '基板を配線する (既定では作業用コピーだけを更新)'),
                       ('check', '事前診断だけ行う (出口の無いパッドなど)。Freerouting は起動しない')):
        p = sub.add_parser(name, help=text)
        p.add_argument('board', help='.kicad_pcb')
        p.add_argument('--config', help='設定ファイル (既定: <基板名>.frplus.json)')
        p.add_argument('--workdir', help='作業フォルダ (既定: <基板名>_frplus)')
    sub.choices['route'].add_argument('--max-passes', type=int, metavar='N',
                                      help='Freerouting 1 回あたりの配線パス数の上限 (設定の max_passes より優先)')
    sub.choices['route'].add_argument('--apply', action='store_true',
                                      help='検証を通ったら元の基板を上書きする (バックアップを作る)')
    p = sub.add_parser('apply', help='route の結果を元の基板に反映する (検証を通っている場合だけ)')
    p.add_argument('workdir', help='route の作業フォルダ')
    sub.add_parser('doctor', help='KiCad、Freerouting、Java が見つかるかを確かめる')
    p = sub.add_parser('install-plugin', help='KiCad のツールバーにボタンを足す (読み込み用ファイルを 1 つ書く)')
    p.add_argument('--dest', help='KiCad のプラグインフォルダ (既定: ドキュメント/KiCad/<版>/scripting/plugins)')
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    try:
        if a.cmd == 'doctor':
            return doctor()
        if a.cmd == 'install-plugin':
            print('書き込みました: ' + install_plugin(a.dest))
            print('KiCad の基板エディターで「ツール → 外部プラグイン → プラグインを更新」を実行してください。')
            return 0
        if a.cmd == 'apply':
            rep = route.apply_result(a.workdir)
            return 0 if rep.get('applied') else 1
        cfg = route.load_config(a.board, a.config)
        if getattr(a, 'max_passes', None):
            cfg['max_passes'] = a.max_passes
        rep = route.run(a.board, cfg, a.workdir, a.apply) if a.cmd == 'route' else route.check(a.board, cfg, a.workdir)
    except tools.ToolError as e:
        print('エラー: %s' % e, file=sys.stderr)
        return 2
    return 0 if rep['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
