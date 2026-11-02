"""命令行面板：列出摄像头、开关录像、调整保留天数与分段时长。"""

from __future__ import annotations

import curses
import unicodedata
from typing import Any, Optional

from .control import ControlError, request

_STATE_TEXT = {
    "starting": "启动中",
    "running": "运行中",
    "not_logged_in": "未登录",
    "error": "异常",
}


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _fit(text: str, width: int) -> str:
    """按终端显示宽度裁剪/补齐，避免中文把表格挤歪。"""
    out = ""
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out += ch
        used += w
    return out + " " * (width - used)


def _size(num: Optional[int]) -> str:
    if not num:
        return "-"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return "-"


class Panel:
    def __init__(self) -> None:
        self.status: Optional[dict[str, Any]] = None
        self.message = ""
        self.cursor = 0
        self.offset = 0
        self.scr = None

    def run(self) -> None:
        curses.wrapper(self._loop)

    # ── 主循环 ───────────────────────────────────────────────
    def _loop(self, stdscr) -> None:
        self.scr = stdscr
        curses.curs_set(0)
        stdscr.timeout(700)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        while True:
            self._fetch()
            self._draw(stdscr)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in (curses.KEY_UP, ord("k")):
                self._move(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self._move(1)
            elif key in (ord(" "), 10, 13, curses.KEY_ENTER):
                self._toggle()
            elif key in (ord("r"), ord("R")):
                self._set_number("retention_days", "保留天数", "天")
            elif key in (ord("s"), ord("S")):
                self._set_number("segment_minutes", "分段时长", "分钟")
            elif key in (ord("d"), ord("D")):
                self._set_number("min_free_gb", "最低剩余空间", "GB")
            elif key in (ord("g"), ord("G")):
                self._set_text("storage_root", "存储目录")
            elif key in (ord("f"), ord("F")):
                self._call("refresh", "已刷新摄像头列表")
            elif key in (ord("x"), ord("X")):
                if self._confirm(stdscr, "停止守护进程（录像会中断）？[y/N] "):
                    try:
                        request("shutdown")
                    except ControlError:
                        pass
                    break

    # ── 数据与绘制 ───────────────────────────────────────────
    def _fetch(self) -> None:
        try:
            self.status = request("status", timeout=2.0)
        except ControlError as exc:
            self.status = None
            self.message = str(exc)

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        status = self.status
        if status is None:
            stdscr.addnstr(0, 0, "MiCamRec 录像面板 — 守护进程未运行", width - 1, curses.A_BOLD)
            stdscr.addnstr(2, 0, "按 q 退出；运行 micam-rec panel 会自动拉起守护进程", width - 1)
            stdscr.clrtoeol()
            stdscr.refresh()
            return

        state = status.get("state", "?")
        disk = status.get("disk") or {}
        cfg = status.get("config") or {}
        head = f"MiCamRec 录像面板  [{_STATE_TEXT.get(state, state)}]  PID {status.get('pid')}"
        stdscr.addnstr(0, 0, head, width - 1, curses.A_BOLD)
        info = (
            f"存储 {cfg.get('storage_root')}  剩余 {disk.get('free_gb')}GB / {disk.get('total_gb')}GB"
            f"  分段 {cfg.get('segment_minutes')}分钟  默认保留 {cfg.get('retention_days')}天"
            f"  画质 {cfg.get('quality')}"
        )
        stdscr.addnstr(1, 0, info, width - 1)
        if status.get("error"):
            stdscr.addnstr(2, 0, f"! {status['error']}", width - 1)

        header = (
            _fit("  ", 3)
            + _fit("摄像头", 24)
            + _fit("状态", 8)
            + _fit("本次写入", 11)
            + _fit("当前段", 10)
            + _fit("保留", 6)
        )
        stdscr.addnstr(3, 0, header, width - 1, curses.A_UNDERLINE)

        cameras = status.get("cameras") or []
        visible = max(1, height - 8)
        self.cursor = max(0, min(self.cursor, len(cameras) - 1)) if cameras else 0
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + visible:
            self.offset = self.cursor - visible + 1

        if not cameras:
            stdscr.addnstr(4, 0, "未发现摄像头（请先登录，并按 F 刷新）", width - 1)
        for idx, cam in enumerate(cameras[self.offset : self.offset + visible]):
            real = self.offset + idx
            writer = cam.get("writer") or {}
            row = (
                "▶ " if real == self.cursor else "  "
            ) + _fit(("● " if cam.get("recording") else "○ ") + str(cam.get("name") or cam.get("did")), 22)
            row += _fit(str(cam.get("connection") or ""), 8)
            row += _fit(_size(writer.get("bytes_written")), 11)
            row += _fit(_size(writer.get("segment_bytes")), 10)
            row += _fit(f"{cam.get('retention_days')}天", 6)
            stdscr.addnstr(4 + idx, 0, row, width - 1, curses.A_REVERSE if real == self.cursor else 0)
            if writer.get("paused_reason"):
                self.message = f"暂停：{writer['paused_reason']}"

        footer_row = height - 2
        keys = "空格 开关录像 | R 保留天数 | S 分段时长 | D 最低剩余 | G 存储目录 | F 刷新 | X 停止守护 | Q 退出"
        stdscr.addnstr(footer_row, 0, keys, width - 1, curses.A_DIM)
        msg = self.message or ""
        stdscr.addnstr(height - 1, 0, msg, width - 1)
        stdscr.clrtoeol()
        stdscr.refresh()

    # ── 操作 ─────────────────────────────────────────────────
    def _move(self, delta: int) -> None:
        cameras = (self.status or {}).get("cameras") or []
        if not cameras:
            return
        self.cursor = max(0, min(self.cursor + delta, len(cameras) - 1))

    def _current_did(self) -> Optional[str]:
        cameras = (self.status or {}).get("cameras") or []
        if not cameras or not (0 <= self.cursor < len(cameras)):
            return None
        return str(cameras[self.cursor].get("did") or "")

    def _toggle(self) -> None:
        did = self._current_did()
        if not did:
            return
        cam = self.status["cameras"][self.cursor]
        cmd = "disable" if cam.get("enabled") else "enable"
        verb = "停止" if cmd == "disable" else "启动"
        self.message = f"{verb} {cam.get('name')} …"
        self._call(cmd, f"{verb} {cam.get('name')} 完成", timeout=20.0, did=did)
        self._fetch()

    def _call(self, cmd: str, ok_message: str, timeout: float = 30.0, **args: Any) -> bool:
        try:
            resp = request(cmd, timeout=timeout, **args)
        except ControlError as exc:
            self.message = str(exc)
            return False
        if not resp.get("ok", True):
            self.message = str(resp.get("error") or "操作失败")
            return False
        self.message = ok_message
        return True

    def _set_number(self, key: str, label: str, unit: str) -> None:
        current = (self.status or {}).get("config", {}).get(key)
        raw = self._prompt(f"{label}（{unit}，当前 {current}，回车保持不变）：")
        if raw is None or not raw.strip():
            self.message = "已取消"
            return
        try:
            value = int(raw.strip())
        except ValueError:
            self.message = "请输入整数"
            return
        if self._call("set", f"{label}已设为 {value}{unit}", **{key: value}):
            self._fetch()

    def _set_text(self, key: str, label: str) -> None:
        current = (self.status or {}).get("config", {}).get(key)
        raw = self._prompt(f"{label}（当前 {current}，回车保持不变）：")
        if raw is None or not raw.strip():
            self.message = "已取消"
            return
        if self._call("set", f"{label}已设为 {raw.strip()}", **{key: raw.strip()}):
            self._fetch()

    def _prompt(self, label: str) -> Optional[str]:
        scr = self.scr
        height, width = scr.getmaxyx()
        # 提示语写倒数第二行，输入从最后一行第 0 列开始：
        # 否则长路径会顶到屏幕右边界被 getstr 截断。
        scr.move(height - 2, 0)
        scr.clrtoeol()
        scr.addnstr(_fit(label, width - 1), width - 1)
        scr.move(height - 1, 0)
        scr.clrtoeol()
        scr.refresh()
        curses.curs_set(1)
        curses.echo()
        # 主循环是 timeout(700) 非阻塞模式，getstr 会沿用该超时导致直接返回空串，
        # 输入这里必须临时切成阻塞模式。
        scr.timeout(-1)
        try:
            raw = scr.getstr(height - 1, 0, width - 2)
        except curses.error:
            return None
        finally:
            curses.noecho()
            curses.curs_set(0)
            scr.timeout(700)
        return raw.decode("utf-8", "ignore").strip()

    def _confirm(self, stdscr, question: str) -> bool:
        height, width = stdscr.getmaxyx()
        stdscr.move(height - 1, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(question, width - 1)
        stdscr.refresh()
        stdscr.nodelay(False)
        key = stdscr.getch()
        stdscr.timeout(700)
        return key in (ord("y"), ord("Y"))


def run_panel() -> int:
    Panel().run()
    return 0
