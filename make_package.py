"""配布用の zip を作る。  python make_package.py [--no-jar]

dist/frplus-<版>.zip に、ツール本体、インストーラ、Freerouting の jar (開発版のビルド) を入れる。
--no-jar を付けると jar を入れない (インストーラが公式リリース v2.5.0 を取得する)。
"""
import glob
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frplus import __version__  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ['install.bat', 'install.ps1', 'README.md', 'NOTICE.txt', 'LICENSE']


def main():
    os.makedirs(os.path.join(ROOT, 'dist'), exist_ok=True)
    out = os.path.join(ROOT, 'dist', 'frplus-%s.zip' % __version__)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in FILES + sorted(glob.glob(os.path.join(ROOT, 'frplus', '*.py'))):
            p = os.path.join(ROOT, f)
            z.write(p, 'frplus/' + os.path.relpath(p, ROOT).replace(os.sep, '/'))
        if '--no-jar' not in sys.argv:
            jars = sorted(glob.glob(os.path.join(ROOT, 'freerouting', 'dev', '*.jar')), reverse=True)[:1]
            for p in jars:      # jar は圧縮済みなので、そのまま格納する
                z.write(p, 'frplus/' + os.path.relpath(p, ROOT).replace(os.sep, '/'), zipfile.ZIP_STORED)
    print('%s (%.1f MB)' % (out, os.path.getsize(out) / 1e6))


if __name__ == '__main__':
    main()
