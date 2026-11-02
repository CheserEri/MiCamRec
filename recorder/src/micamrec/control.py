"""守护进程与命令行面板之间的 Unix socket 控制协议。

一行一个 JSON 请求，一行一个 JSON 响应。没有额外依赖，也不暴露到网络上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from .config import log_path, socket_path

_LOGGER = logging.getLogger(__name__)


class ControlError(RuntimeError):
    """控制请求失败（守护进程未运行或返回错误）。"""


class ControlServer:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = socket_path()
        if path.exists():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._on_client, path=str(path))
        os.chmod(path, 0o600)
        _LOGGER.info("control socket listening, %s", path)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        path = socket_path()
        try:
            path.unlink()
        except OSError:
            pass

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(line or b"{}")
            response = await self.engine.handle(
                str(request.get("cmd") or ""), request.get("args") or {}
            )
            if not isinstance(response, dict):
                response = {"ok": True, "data": response}
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": str(exc)}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except (BrokenPipeError, ConnectionError):
            pass
        finally:
            writer.close()


def request(cmd: str, timeout: float = 5.0, **args: Any) -> dict[str, Any]:
    """向守护进程发一条命令并返回响应。"""
    path = socket_path()
    if not path.exists():
        raise ControlError("守护进程未运行")
    payload = json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            sock.sendall(payload)
            chunks = []
            while b"\n" not in b"".join(chunks):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, socket.timeout) as exc:
            raise ControlError(f"与控制进程通信失败: {exc}") from exc
    raw = b"".join(chunks)
    if not raw:
        raise ControlError("控制进程没有返回数据")
    return json.loads(raw.split(b"\n", 1)[0])


def is_running() -> bool:
    try:
        return bool(request("status", timeout=2.0).get("ok"))
    except ControlError:
        return False


def spawn_daemon(wait_seconds: float = 25.0) -> bool:
    """在后台拉起守护进程，等控制 socket 就绪。"""
    if is_running():
        return True
    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as fp:
        fp.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} start daemon ===\n".encode())
        fp.flush()
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "micamrec", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=fp,
            stderr=fp,
            close_fds=True,
            start_new_session=True,
        )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(0.3)
    return False
