#!/usr/bin/env python3
"""一键启动 UI：起 FastAPI 服务并自动打开浏览器。

用法:
    .venv/bin/python run_ui.py            # 默认端口 8618
    .venv/bin/python run_ui.py --port 9000
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def wait_port(port: int, timeout: float = 15.0) -> bool:
    for _ in range(int(timeout * 10)):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8618)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import uvicorn
    from server.app import app   # noqa: F401  (导入即校验)

    url = f"http://127.0.0.1:{args.port}/"

    if not args.no_browser:
        threading.Thread(
            target=_open_when_ready, args=(url, args.port), daemon=True
        ).start()

    print(f"pdf-translator UI → {url}  (Ctrl+C 退出)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _open_when_ready(url: str, port: int) -> None:
    if wait_port(port):
        if not webbrowser.open(url):
            # WSL 兜底：webbrowser 在无 X 环境可能失败，直接调 Windows 侧浏览器
            try:
                import subprocess
                subprocess.Popen(["/mnt/c/Windows/System32/cmd.exe", "/c",
                                  "start", url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                print(f"已在 Windows 浏览器打开 {url}")
            except OSError as e:
                print(f"自动开浏览器失败（{e}），请手动访问 {url}")


if __name__ == "__main__":
    raise SystemExit(main())
