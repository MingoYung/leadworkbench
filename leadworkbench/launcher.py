"""Executable entry: arguments use the CLI; no arguments launch the desktop UI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadworkbench.cli import ApiClient, ApiError, DEFAULT_PORT, default_data_dir, main as cli_main


def ensure_service(data_dir: Path | None = None, port: int = DEFAULT_PORT) -> dict:
    directory = (data_dir or default_data_dir()).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    client = ApiClient(directory, timeout=1)
    try:
        client.request("GET", "/api/health")
        url, _ = client.connection()
    except ApiError:
        if not 1 <= port <= 65535:
            raise ApiError("ARGUMENT", "port must be between 1 and 65535.")
        if getattr(sys, "frozen", False):
            primary = Path(sys.executable).resolve()
            if primary.stem.lower() == "leadworkbenchdesktop":
                options = [primary.parent.parent / "LeadWorkbench" / "LeadWorkbench.exe", primary.with_name("LeadWorkbench.exe")]
                primary = next((path for path in options if path.is_file()), options[0])
                if not primary.is_file():
                    raise ApiError("INSTALLATION", "Keep the desktop launcher beside the LeadWorkbench executable distribution.")
            command = [str(primary), "--data-dir", str(directory), "serve", "--port", str(port), "--no-browser"]
            working_dir = primary.parent
        else:
            command = [sys.executable, "-m", "leadworkbench", "--data-dir", str(directory), "serve", "--port", str(port), "--no-browser"]
            working_dir = Path(__file__).resolve().parent.parent
        with (directory / "server-stdout.log").open("ab") as out, (directory / "server-stderr.log").open("ab") as err:
            child = subprocess.Popen(command, cwd=working_dir, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                client.request("GET", "/api/health")
                url, _ = client.connection()
                break
            except ApiError:
                if child.poll() is not None:
                    raise ApiError("START_FAILED", "Application did not start. Check data/server-stderr.log; the chosen port may already be in use.") from None
                time.sleep(0.2)
        else:
            raise ApiError("START_TIMEOUT", "Application is still starting. Check data/server-stderr.log before launching it again.")
    return {"url": url + "/", "data_dir": str(directory), "browser_open_requested": False}


def launch_desktop(data_dir: Path | None = None, port: int = DEFAULT_PORT, open_browser: bool = True) -> dict:
    """Start the local API, then run the native window. False preserves headless callers."""
    if open_browser and getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() != "leadworkbenchdesktop":
        root = Path(sys.executable).resolve().parent
        desktop = root.parent / "LeadWorkbenchDesktop" / "LeadWorkbenchDesktop.exe"
        if not desktop.is_file():
            raise ApiError("INSTALLATION", "原生桌面程序缺失，请保留完整 LeadWorkbenchDesktop 文件夹。")
        directory = (data_dir or default_data_dir()).expanduser().resolve()
        subprocess.Popen([str(desktop), "desktop", "--data-dir", str(directory), "--port", str(port)],
                         cwd=desktop.parent, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
        return {"data_dir": str(directory), "interface": "native_qt_widgets", "native_window_started": True, "browser_open_requested": False}
    info = ensure_service(data_dir, port)
    if not open_browser:
        return {**info, "interface": "service_only"}
    try:
        from leadworkbench.desktop_runtime import run_native_app
        code = run_native_app(Path(info["data_dir"]), info)
    except ImportError as exc:
        import traceback
        with (Path(info["data_dir"]) / "desktop-error.log").open("a", encoding="utf-8") as log:
            traceback.print_exc(file=log)
        raise ApiError("DESKTOP_DEPENDENCY", "桌面组件缺失，请使用完整原生桌面程序包；源码开发需安装 requirements-desktop.txt。") from exc
    return {**info, "interface": "native_qt_widgets", "desktop_exit_code": code}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] != "desktop":
        return cli_main(arguments)
    parser = argparse.ArgumentParser(prog="LeadWorkbench desktop", description="获客平台桌面启动器")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--service-only", "--no-browser", dest="service_only", action="store_true", help="只启动本机 API，供 CLI/MCP 使用")
    options = parser.parse_args(arguments[1:] if arguments else [])
    try:
        result = launch_desktop(options.data_dir, options.port, not options.service_only)
        if sys.stdout is not None:
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return int(result.get("desktop_exit_code", 0))
    except ApiError as exc:
        if sys.stderr is not None:
            print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, exc.message, "获客平台启动失败", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
