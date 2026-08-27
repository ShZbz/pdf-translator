#!/usr/bin/env python3
"""一键启动 UI：起 FastAPI 服务并自动打开浏览器。

用法:
    .venv/bin/python run_ui.py            # 默认端口 8618
    .venv/bin/python run_ui.py --port 9000
"""
from __future__ import annotations

import argparse
import random
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


def _pick_port(port: int) -> int:
    """端口可用性预探测：被占/落进 Windows 保留段时换高位随机端口。

    Windows 上 Hyper-V/WINNAT 会保留大段临时端口（实测 86xx 段命中），
    bind 报 WSAEADDRINUSE 但 netstat 看不到监听者——用户侧表现为
    「端口没被占却起不来」。预探测成功才交给 uvicorn（本地单用户
    工具，探测与 serve 间的极小竞争窗口可接受）。
    """
    for cand in [port] + [random.randint(20000, 24000) for _ in range(5)]:
        try:
            with socket.create_server(("127.0.0.1", cand)):
                return cand
        except OSError:
            continue
    return port


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8618)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import uvicorn  # noqa: F401  (导入即校验)
    from server.app import app   # noqa: F401  (导入即校验)

    actual = _pick_port(args.port)
    if actual != args.port:
        print(f"端口 {args.port} 不可用（可能被占或位于 Windows 保留段），"
              f"改用 {actual}")
    url = f"http://127.0.0.1:{actual}/"

    if not args.no_browser:
        threading.Thread(
            target=_open_when_ready, args=(url, actual), daemon=True
        ).start()

    print(f"pdf-translator UI → {url}  (Ctrl+C 退出)")
    uvicorn.run(app, host="127.0.0.1", port=actual, log_level="warning")
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
