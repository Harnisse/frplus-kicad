"""KiCad のツールバーボタン (アクションプラグイン)。中身は `python -m frplus route` を別プロセスで呼ぶだけ。

KiCad のプラグインフォルダには `python -m frplus install-plugin` が書く数行の読み込み用ファイルを置く。
"""
import os
import subprocess
import threading

import pcbnew
import wx

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _python():
    """KiCad 付属の python.exe (KiCad の中では sys.executable が kicad.exe になるので使えない)。"""
    from . import tools
    return os.path.join(tools.find_kicad_bin(), 'python.exe')


def _spawn(args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return subprocess.Popen([_python(), '-m', 'frplus'] + args, cwd=TOOL_DIR, env=env, creationflags=flags,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace')


class RunDialog(wx.Dialog):
    def __init__(self, parent, board_path):
        super().__init__(parent, title='frplus: Freerouting で配線', size=(760, 460),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board_path = board_path
        self.workdir = os.path.splitext(board_path)[0] + '_frplus'
        self.proc = None
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.apply_btn = wx.Button(self, label='基板に反映')
        self.close_btn = wx.Button(self, wx.ID_CLOSE, '中止')
        self.apply_btn.Disable()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        row.Add(self.apply_btn, 0, wx.RIGHT, 8)
        row.Add(self.close_btn)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.log, 1, wx.EXPAND | wx.ALL, 8)
        box.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(box)
        self.apply_btn.Bind(wx.EVT_BUTTON, self.on_apply)
        self.close_btn.Bind(wx.EVT_BUTTON, self.on_close)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.start(['route', board_path], self.route_done)

    def start(self, args, done):
        self.proc = _spawn(args)
        threading.Thread(target=self.pump, args=(self.proc, done), daemon=True).start()

    def pump(self, proc, done):
        for line in proc.stdout:
            wx.CallAfter(self.log.AppendText, line)
        wx.CallAfter(done, proc.wait())

    def route_done(self, code):
        self.proc = None
        self.close_btn.SetLabel('閉じる')
        if code == 0:
            self.log.AppendText('\n検証を通りました。「基板に反映」で元の基板を上書きします (バックアップを作ります)。\n')
            self.apply_btn.Enable()
        else:
            self.log.AppendText('\n検証を通らなかったので、元の基板は変更していません。結果は作業フォルダで確認できます。\n')

    def on_apply(self, _evt):
        self.apply_btn.Disable()
        self.start(['apply', self.workdir], self.apply_done)

    def apply_done(self, code):
        self.proc = None
        if code == 0:
            self.log.AppendText('\n反映しました。基板エディターで「ファイル → 元に戻す」を実行して読み込み直してください。\n')

    def on_close(self, _evt):
        if self.proc is not None:
            self.proc.kill()
        self.Destroy()


class FrplusAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'frplus: Freerouting で配線'
        self.category = 'Routing'
        self.description = '入力を整えて Freerouting で配線し、DRC で検証してから反映する'
        self.show_toolbar_button = True

    def Run(self):
        path = pcbnew.GetBoard().GetFileName()
        if not path:
            wx.MessageBox('先に基板を保存してください。', 'frplus')
            return
        msg = ('保存済みの基板ファイルを配線します。未保存の変更は使われません。\n\n%s\n\n'
               'ロックした配線はそのまま残り、それ以外は引き直されます。続けますか?' % path)
        if wx.MessageBox(msg, 'frplus', wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            RunDialog(None, path).Show()


def register():
    FrplusAction().register()
