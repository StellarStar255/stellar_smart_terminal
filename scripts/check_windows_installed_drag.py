"""Verify the installed Windows application, ConPTY and the system clipboard."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from pywinauto import Application, clipboard, keyboard, mouse
from pywinauto.timings import wait_until


def main(executable):
    with tempfile.TemporaryDirectory(prefix="stellar-drag-") as directory:
        root = Path(directory)
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/transcript_mouse_viewer.py"
        shutil.copyfile(fixture, root / "viewer.py")
        # Let ConPTY receive the final window dimensions before drawing.
        (root / "start_viewer.py").write_text(
            "import pathlib, runpy, time\n"
            "while not pathlib.Path('ready').exists(): time.sleep(.05)\n"
            "runpy.run_path('viewer.py', run_name='__main__')\n")
        config = {
            "onboarding_shown": True, "workspace_restore_enabled": False,
            "last_preset_index": 0,
            "presets": [{"name": "Clipboard test", "commands": ["python -u start_viewer.py wheel.json"]}],
        }
        (root / ".smart_terminal_config.json").write_text(json.dumps(config))
        report = root / "wheel.json"
        process = subprocess.Popen([executable, "--working-dir", directory],
                                   env=dict(os.environ, STELLAR_DATA_DIR=directory, QT_QPA_PLATFORM="windows"))
        try:
            app = Application(backend="win32").connect(process=process.pid)
            window = app.window(class_name_re="Qt.*QWindowIcon", visible_only=True)
            window.wait("visible", timeout=45)
            window.move_window(x=8, y=8, width=900, height=580)
            window.set_focus()
            (root / "ready").touch()
            wait_until(45, 0.1, report.exists)
            rect = window.rectangle()
            x = rect.left + int(.70 * rect.width())
            rows = json.loads(report.read_text())["rows"]
            mouse.press(coords=(x, rect.top + int(.62 * rect.height())))
            try:
                mouse.move(coords=(x, rect.bottom + 12))
                wait_until(30, .1, lambda: json.loads(report.read_text())["offset"] >= 100 + 2 * rows)
                mouse.move(coords=(x, rect.top + int(.69 * rect.height())))
            finally:
                mouse.release(coords=(x, rect.top + int(.69 * rect.height())))
            clipboard.EmptyClipboard()
            keyboard.send_keys("^c")
            wait_until(10, .1, lambda: 13 in clipboard.GetClipboardFormats())
            interior = clipboard.GetData().splitlines()[1:-1]
            if len(interior) <= rows:
                raise AssertionError(f"Only {len(interior)} complete copied rows for {rows} visible rows")
            first = int(interior[0].split()[1])
            expected = [f"row {n:04d} 中文 sample" for n in range(first, first + len(interior))]
            if interior != expected:
                raise AssertionError("Copied transcript contains missing, altered or out-of-order rows")
            print(f"Installed Windows app copied {len(interior)} consecutive rows via ConPTY")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main(sys.argv[1])
