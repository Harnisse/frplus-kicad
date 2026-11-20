"""KiCad の外でボタンのダイアログを動かす (KiCad 付属 python で実行)。クリックの代わりにハンドラを直接呼ぶ。

  python.exe gui_harness.py <board.kicad_pcb>

配線 → 「基板に反映」まで進め、ダイアログに出たログを標準出力に書く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wx  # noqa: E402

from frplus import kicad_plugin as k  # noqa: E402

app = wx.App()
dlg = k.RunDialog(None, os.path.abspath(sys.argv[1]))
dlg.Show()
state = {'applied': False, 'ticks': 0}


def tick(_evt):
    state['ticks'] += 1
    text = dlg.log.GetValue()
    done = dlg.proc is None
    if done and not state['applied'] and dlg.apply_btn.IsEnabled():
        state['applied'] = True
        dlg.on_apply(None)                      # 「基板に反映」を押したのと同じ
        return
    if (done and (state['applied'] or not dlg.apply_btn.IsEnabled())) or state['ticks'] > 600:
        sys.stdout.buffer.write(text.encode('utf-8'))
        print('\n[harness] apply pressed: %s, close label: %s' % (state['applied'], dlg.close_btn.GetLabel()))
        timer.Stop()
        dlg.on_close(None)
        app.ExitMainLoop()


timer = wx.Timer(dlg)
dlg.Bind(wx.EVT_TIMER, tick, timer)
timer.Start(1000)
app.MainLoop()
