from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

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


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="SixDeck",
        description="SixDeck Community Launcher — GUI or direct headless profile launcher.",
    )
    parser.add_argument(
        "--launch",
        metavar="PROFILE",
        default="",
        help="Directly launch a profile (ID or name, e.g. 'vanilla', 'perf-60') without opening the GUI.",
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="Launch the last validated runtime in Safe Mode (Vanilla, 720p, 75%% scale, Ray Tracing OFF).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Start in strict Offline Mode (disables 100%% of network requests and scrapers).",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List all available SixDeck profiles and exit.",
    )
    parser.add_argument(
        "--hardware",
        action="store_true",
        help="Print detected PC hardware diagnostics (GPU, RAM, Pagefile/Swap, AVX2, Admin, Proton, warnings) and exit.",
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="Run resumable shader & GPU pipeline precompilation with exact Play env parity and exit.",
    )
    parser.add_argument(
        "--fetch-sdk",
        action="store_true",
        help="Fetch portable NVIDIA DLSS / AMD FSR 3 SDK DLLs for the detected GPU and exit.",
    )
    return parser.parse_args(argv)


def run_cli_headless(args: argparse.Namespace) -> int:
    config = gui.load_config(gui.CONFIG_PATH)
    controller = gui.TrackerController(config, skip_warmup=True)

    if args.offline:
        controller.toggle_offline_mode(True)

    if args.hardware:
        hw = controller.get_hardware_profile(force=True)
        print(
            f"[SixDeck] {hw['os']} {hw['release']} | GPU: {hw['gpu_name']} ({hw['gpu_vendor_name']}) | "
            f"RAM: {hw['ram_gb']} GB | Pagefile/Swap: {hw['pagefile_gb']} GB | AVX2: {hw['avx2']} | Admin: {hw['is_admin']}"
        )
        if hw.get("proton_runner"):
            pr = hw["proton_runner"]
            print(f"[SixDeck] Linux Windows Runtime: {pr['kind']} ({pr['path']})")
        for w in hw.get("warnings") or []:
            print(f"[SixDeck] WARNING ({w['title']}): {w['message']}")
        return 0

    if args.list_profiles:
        info = controller.list_profiles()
        active = info.get("active_profile")
        for p in info.get("profiles") or []:
            mark = "*" if p.get("id") == active else " "
            print(f"{mark} {p.get('id'):<18} {p.get('name')} — {p.get('description', '')}")
        return 0

    if args.fetch_sdk:
        res = controller.fetch_upscaler_sdks()
        if not res.get("ok"):
            print(f"[SixDeck] SDK fetch failed: {res.get('error')}", file=sys.stderr)
            return 1
        print(f"[SixDeck] {res.get('message')}")
        return 0

    if args.prewarm:
        res = controller.prewarm_shaders()
        if not res.get("ok"):
            print(f"[SixDeck] Prewarm failed: {res.get('error')}", file=sys.stderr)
            return 1
        print(f"[SixDeck] {res.get('message')}")
        while controller._prewarm_state == "running":
            time.sleep(0.5)
        print(f"[SixDeck] Prewarm finished ({controller._prewarm_state}): {controller._prewarm_detail}")
        return 0 if controller._prewarm_state == "done" else 1

    target_profile = (args.launch or "").strip()
    if target_profile:
        sw = controller.switch_profile(target_profile)
        if not sw.get("ok"):
            print(f"[SixDeck] Error: {sw.get('error')}", file=sys.stderr)
            return 2

    res = controller.play_game(safe_mode=bool(args.safe_mode))
    if not res.get("ok"):
        print(f"[SixDeck] Launch failed: {res.get('error')}", file=sys.stderr)
        return 1

    print(f"[SixDeck] {res.get('message')}")
    proc = controller._game_proc
    if proc is not None:
        return int(proc.wait())
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_cli_args(argv)
    if args.list_profiles or args.launch or args.safe_mode or args.hardware or args.prewarm or args.fetch_sdk:
        raise SystemExit(run_cli_headless(args))

    import webview

    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SixDeck.Community.Launcher.1")
        except Exception:
            pass
    else:
        # Avoid Chromium setuid sandbox crash inside Linux/Crostini containers when using QtWebEngine
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu-sandbox")

    config = gui.load_config(gui.CONFIG_PATH)
    controller = gui.TrackerController(config)
    if args.offline:
        controller.toggle_offline_mode(True)

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
    if os.name != "nt":
        try:
            import qtpy  # noqa: F401

            start_kwargs["gui"] = "qt"
        except ImportError:
            pass
    if ico_path.exists():
        start_kwargs["icon"] = str(ico_path)
    webview.start(**start_kwargs)
    os._exit(0)


if __name__ == "__main__":
    main()
