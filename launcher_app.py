from __future__ import annotations

import ctypes
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import webview

import gui

# Fixed 16:9 desktop window ratio
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 675
WINDOW_TITLE = "SixDeck"


def _apply_windows_icon(title: str, ico_path: Path) -> None:
    """Explicitly sets both small (titlebar) and big (taskbar/Alt+Tab) Win32 icons on Windows."""
    if os.name != "nt" or not ico_path.is_file():
        return
    try:
        user32 = ctypes.windll.user32
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        hicon_small = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        hicon_big = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        if not hicon_small and not hicon_big:
            return

        # Poll briefly until the native WebView2 window handle is created
        for _ in range(40):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                if hicon_small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                if hicon_big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                return
            time.sleep(0.1)
    except Exception:
        pass


def main() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SixDeck.Community.Launcher.1")
        except Exception:
            pass

    config = gui.load_config(gui.CONFIG_PATH)
    controller = gui.TrackerController(config)

    host = "127.0.0.1"
    server: ThreadingHTTPServer | None = None
    chosen_port = 8765
    for port in range(8765, 8795):
        try:
            server = ThreadingHTTPServer((host, port), gui.make_handler(controller))
            chosen_port = port
            break
        except OSError:
            continue

    if server is None:
        raise RuntimeError("No free local port between 8765 and 8794.")

    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    url = f"http://{host}:{chosen_port}"
    ico_path = gui.ASSETS_DIR / "launcher_icon.ico"

    window = webview.create_window(
        title=WINDOW_TITLE,
        url=url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        resizable=False,
        background_color="#0c0818",
        text_select=True,
    )

    def _on_shown() -> None:
        threading.Thread(target=_apply_windows_icon, args=(WINDOW_TITLE, ico_path), daemon=True).start()

    def _on_closed() -> None:
        try:
            controller.stop()
        except Exception:
            pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    window.events.shown += _on_shown
    window.events.closed += _on_closed

    start_kwargs: dict[str, object] = {"debug": False}
    if ico_path.exists():
        start_kwargs["icon"] = str(ico_path)
    webview.start(**start_kwargs)
    os._exit(0)


if __name__ == "__main__":
    main()
