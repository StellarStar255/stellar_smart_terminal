# -*- coding: utf-8 -*-
"""测试辅助：清掉 Qt 的 quitNow 粘滞状态（2026-09 审查测试共用）。

Qt 6.5+ 的 QCoreApplication::quit() / QEvent::Quit 最终调 exit()：没有事件
循环在跑时它只把线程数据里的 quitNow 置 True 且不会自动复位，之后同进程里
任何 QDialog.exec() / QEventLoop.exec() 都立刻返回 -1（已用探针证实）。
只有 QCoreApplication::exec() 会在进出时把 quitNow 复位——这里跑一个立刻
退出的 exec() 把状态洗干净，供关窗类测试在收尾时调用。
"""
from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtWidgets import QApplication


def reset_quit_state(app):
    QTimer.singleShot(0, app.exit)
    app.exec()
    for _ in range(3):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def dispose_windows(app, wins):
    """强制关掉并销毁测试建的 MainWindow（未显示窗口 close() 不走 closeEvent，
    统一 sendEvent(QCloseEvent)）。"""
    from PyQt6 import sip
    from PyQt6.QtGui import QCloseEvent
    for w in wins:
        if sip.isdeleted(w):
            continue
        if not w._closing_in_progress:
            w._force_closing = True
            QApplication.sendEvent(w, QCloseEvent())
        w.deleteLater()
    reset_quit_state(app)
