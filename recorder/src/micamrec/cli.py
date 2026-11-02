"""命令行入口：登录、守护进程、面板与常用运维命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from . import auth
from .config import (
    AuthStore,
    ConfigStore,
    app_dir,
    auth_path,
    camera_folder,
    config_path,
    log_path,
    socket_path,
)
from .control import ControlError, is_running, request, spawn_daemon

_LOGGER = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S")
        )
        root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(
            log_path(), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
    except OSError:
        pass
    logging.getLogger("miot").setLevel(logging.INFO if verbose else logging.WARNING)
    logging.getLogger("micamrec").setLevel(level)


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


# ── 子命令实现 ────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    cfg = ConfigStore().load()
    if args.storage:
        Path(args.storage).expanduser().mkdir(parents=True, exist_ok=True)
        cfg.storage_root = args.storage
    if args.segment:
        cfg.segment_minutes = max(1, int(args.segment))
    if args.retention:
        cfg.retention_days = max(1, int(args.retention))
    ConfigStore().save(cfg)
    print(f"配置目录: {app_dir()}")
    print(f"配置文件: {config_path()}")
    print(f"存储目录: {cfg.storage_root}")
    print(f"分段时长: {cfg.segment_minutes} 分钟")
    print(f"保留天数: {cfg.retention_days} 天")
    return 0


async def _login_async(args: argparse.Namespace) -> int:
    store = AuthStore()
    client, url = await auth.make_oauth_url(store)
    try:
        print("\n请在浏览器中打开下面的链接完成小米账号授权：\n")
        print(f"  {url}\n")
        if args.url_only:
            print("授权完成后运行 micam-rec login，并粘贴回调页给出的授权码。")
            return 0
        payload = args.payload
        if not payload:
            if not sys.stdin.isatty():
                print("当前不是交互终端，请使用 --payload 传入授权码。", file=sys.stderr)
                return 2
            payload = input("把回调页面的授权码粘贴到这里后回车: ")
        await auth.complete_login(client, store, payload)
        print(f"登录成功，凭据已保存到 {auth_path()}")
        return 0
    finally:
        await client.deinit_async()


async def _list_cameras_async() -> list[dict[str, Any]]:
    store = AuthStore()
    if not auth.logged_in(store):
        raise RuntimeError("尚未登录，请先运行 micam-rec login")
    client = await auth.build_client(store)
    try:
        cameras = await client.get_cameras_async()
    finally:
        await client.deinit_async()
    cfg = ConfigStore().load()
    rows = []
    for index, (did, info) in enumerate(cameras.items(), start=1):
        cam_cfg = cfg.cameras.get(did)
        rows.append(
            {
                "index": index,
                "did": did,
                "name": (cam_cfg.name if cam_cfg and cam_cfg.name else info.name),
                "enabled": bool(cam_cfg.enabled) if cam_cfg else False,
                "lan_online": info.lan_online,
                "local_ip": info.local_ip,
                "channels": info.channel_count,
            }
        )
    return rows


def cmd_cameras(args: argparse.Namespace) -> int:
    if is_running():
        status = request("status")
        rows = [
            {
                "index": index,
                "did": cam["did"],
                "name": cam["name"],
                "enabled": cam["enabled"],
                "lan_online": None,
                "local_ip": "",
                "channels": "",
                "recording": cam["recording"],
                "connection": cam["connection"],
            }
            for index, cam in enumerate(status.get("cameras") or [], start=1)
        ]
    else:
        try:
            rows = asyncio.run(_list_cameras_async())
        except Exception as exc:  # noqa: BLE001
            print(f"获取摄像头失败: {exc}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("没有发现摄像头。")
        return 0
    print(f"{'#':>2}  {'状态':<4} {'名称':<24} {'局域网':<6} {'IP':<16} DID")
    for row in rows:
        flag = "录制" if row.get("recording") else ("启用" if row.get("enabled") else "停用")
        lan = "" if row.get("lan_online") is None else ("在线" if row["lan_online"] else "离线")
        print(
            f"{row['index']:>2}  {flag:<4} {str(row['name'])[:24]:<24} {lan:<8}"
            f"{str(row.get('local_ip') or ''):<16} {row['did']}"
        )
    return 0


def _resolve_camera(token: str) -> str:
    try:
        status = request("status")
    except ControlError as exc:
        raise SystemExit(f"{exc}，请先启动守护进程（micam-rec panel 或 serve）") from exc
    cameras = status.get("cameras") or []
    for cam in cameras:
        if token in (cam.get("did"), cam.get("name")):
            return str(cam["did"])
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(cameras):
            return str(cameras[index - 1]["did"])
        raise SystemExit(f"序号 {index} 超出范围")
    for cam in cameras:
        if token in str(cam.get("name") or "") or token in str(cam.get("did") or ""):
            return str(cam["did"])
    raise SystemExit(f"未找到摄像头 {token}")


def cmd_enable(args: argparse.Namespace) -> int:
    did = _resolve_camera(args.camera)
    resp = request("enable", timeout=30.0, did=did)
    print("已开始录制" if resp.get("ok") else f"启动失败: {resp.get('error')}")
    return 0 if resp.get("ok") else 1


def cmd_disable(args: argparse.Namespace) -> int:
    did = _resolve_camera(args.camera)
    resp = request("disable", timeout=30.0, did=did)
    print("已停止录制" if resp.get("ok") else f"停止失败: {resp.get('error')}")
    return 0 if resp.get("ok") else 1


def _apply_set(key: str, value: Any) -> int:
    resp = request("set", **{key: value})
    if not resp.get("ok", True):
        print(f"设置失败: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{key} = {value}")
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    return _apply_set("retention_days", int(args.days))


def cmd_segment(args: argparse.Namespace) -> int:
    return _apply_set("segment_minutes", int(args.minutes))


def cmd_quality(args: argparse.Namespace) -> int:
    return _apply_set("quality", args.level)


def cmd_disk(args: argparse.Namespace) -> int:
    cfg = ConfigStore().load()
    root = Path(cfg.storage_root).expanduser()
    if not root.exists():
        print(f"存储目录不存在: {root}", file=sys.stderr)
        return 1
    usage = shutil.disk_usage(root)
    print(f"存储目录: {root}")
    print(f"剩余空间: {_human_size(usage.free)} / {_human_size(usage.total)}")
    total = 0
    for did, cam_cfg in cfg.cameras.items():
        cam_dir = root / camera_folder(did, cam_cfg.name)
        size = 0
        if cam_dir.exists():
            for path in list(cam_dir.rglob("*.mp4")) + list(cam_dir.rglob("*.mkv")):
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
        total += size
        print(
            f"  {str(cam_cfg.name or did)[:20]:<20} {_human_size(size):>10}"
            f"  保留 {cfg.retention_for(did)} 天"
        )
    print(f"合计录像占用: {_human_size(total)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        status = request("status")
    except ControlError as exc:
        print(f"{exc}。运行 micam-rec panel 自动拉起，或 micam-rec serve 前台运行。")
        return 1
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    cfg = status.get("config") or {}
    disk = status.get("disk") or {}
    print(f"状态: {status.get('state')}  PID: {status.get('pid')}  运行 {status.get('uptime')}s")
    if status.get("error"):
        print(f"错误: {status['error']}")
    print(
        f"存储: {cfg.get('storage_root')}  剩余 {disk.get('free_gb')}GB"
        f"  分段 {cfg.get('segment_minutes')} 分钟  保留 {cfg.get('retention_days')} 天"
    )
    cameras = status.get("cameras") or []
    if not cameras:
        print("摄像头: 无")
    for cam in cameras:
        writer = cam.get("writer") or {}
        print(
            f"  [{'录制' if cam['recording'] else '停止'}] {cam['name']} ({cam['did']})"
            f" 连接:{cam['connection']} 本次写入:{_human_size(writer.get('bytes_written') or 0)}"
            f" 保留:{cam['retention_days']}天"
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    from .engine import Engine

    engine = Engine()
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    if not is_running():
        print("守护进程未运行，正在后台启动…")
        if not spawn_daemon():
            print(
                f"启动失败，请查看日志 {log_path()}，或直接前台运行 micam-rec serve 排查。",
                file=sys.stderr,
            )
            return 1
    from .tui import run_panel

    return run_panel()


def cmd_stop(args: argparse.Namespace) -> int:
    try:
        request("shutdown")
    except ControlError as exc:
        print(str(exc))
        return 1
    print("已请求守护进程退出")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    AuthStore().clear_oauth()
    print("已清除登录凭据")
    return 0


# ── 参数解析 ──────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="micam-rec",
        description="小米摄像头录像服务：原始码流直录、按关键帧分段、按天保留。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化配置")
    p_init.add_argument("--storage", help="录像存储目录")
    p_init.add_argument("--segment", type=int, help="分段时长（分钟）")
    p_init.add_argument("--retention", type=int, help="保留天数")
    p_init.set_defaults(func=cmd_init)

    p_login = sub.add_parser("login", help="登录小米账号（OAuth）")
    p_login.add_argument("--url-only", action="store_true", help="只打印授权 URL")
    p_login.add_argument("--payload", help="回调页面的授权码（base64）或回调 URL")
    p_login.set_defaults(func=lambda args: asyncio.run(_login_async(args)))

    p_logout = sub.add_parser("logout", help="清除登录凭据")
    p_logout.set_defaults(func=cmd_logout)

    p_cams = sub.add_parser("cameras", help="列出摄像头")
    p_cams.add_argument("--json", action="store_true")
    p_cams.set_defaults(func=cmd_cameras)

    p_serve = sub.add_parser("serve", help="前台运行守护进程")
    p_serve.add_argument("--verbose", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_panel = sub.add_parser("panel", help="打开命令行面板（必要时自动拉起守护进程）")
    p_panel.set_defaults(func=cmd_panel)

    p_status = sub.add_parser("status", help="查看守护进程与录制状态")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    for name, help_text, func in (
        ("enable", "开始录制某路摄像头", cmd_enable),
        ("disable", "停止录制某路摄像头", cmd_disable),
    ):
        sub_p = sub.add_parser(name, help=help_text)
        sub_p.add_argument("camera", help="摄像头序号（cameras 输出里的 #）、名称或 DID")
        sub_p.set_defaults(func=func)

    p_ret = sub.add_parser("retention", help="设置保留天数")
    p_ret.add_argument("days", type=int)
    p_ret.set_defaults(func=cmd_retention)

    p_seg = sub.add_parser("segment", help="设置分段时长（分钟）")
    p_seg.add_argument("minutes", type=int)
    p_seg.set_defaults(func=cmd_segment)

    p_quality = sub.add_parser("quality", help="设置画质（low / high）")
    p_quality.add_argument("level", choices=["low", "high"])
    p_quality.set_defaults(func=cmd_quality)

    p_disk = sub.add_parser("disk", help="查看存储占用")
    p_disk.set_defaults(func=cmd_disk)

    p_stop = sub.add_parser("stop", help="停止守护进程")
    p_stop.set_defaults(func=cmd_stop)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(app_dir(), exist_ok=True)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
