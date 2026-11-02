"""原始码流切段与封装。

录制不做解码、不重编码：小米 SDK 回调给出的是 Annex-B 形式的 H.264 / H.265
码流，这里按关键帧切段，再把每一段通过管道交给 ffmpeg 做 `-c copy` 封装成
MKV。MKV 对断电/进程被杀比 MP4 友好，未正常收尾的段通常仍可播放。
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

# H.264 / H.265 的 NAL 单元类型。
_H264_NON_IDR = 1
_H264_IDR = 5
_H264_SPS = 7
_H264_PPS = 8
_H265_VPS = 32
_H265_SPS = 33
_H265_PPS = 34
_H265_IRAP = frozenset({16, 17, 18, 19, 20, 21})
_H265_PARAM_OR_IRAP = frozenset({_H265_VPS, _H265_SPS, _H265_PPS}) | _H265_IRAP

_QUEUE_MAX = 240

# 原始码流不带 PTS，ffmpeg 靠 -framerate 给每帧生成时间戳，所以必须接近真实帧率，
# 否则文件时长会明显偏短/偏长。相机是 VFR（约 15fps），这里用回调的 ts 实测。
_FPS_WINDOW = 150  # 参与统计的最近帧间隔数量
_FPS_MIN = 4.0
_FPS_MAX = 60.0
_FPS_FALLBACK = 15.0
_FPS_WARMUP_SECONDS = 10.0


def split_nals(data: bytes) -> list[tuple[int, int, int]]:
    """拆分 Annex-B 码流的 NAL 单元。

    返回 ``[(start, end, nal_header_byte), ...]``，切片 ``data[start:end]``
    自带起始码，可直接写回管道。
    """
    n = len(data)
    positions: list[tuple[int, int]] = []
    i = 0
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0:
            if i + 3 < n and data[i + 2] == 0 and data[i + 3] == 1:
                positions.append((i, 4))
                i += 4
                continue
            if data[i + 2] == 1:
                positions.append((i, 3))
                i += 3
                continue
        i += 1
    if not positions:
        return []
    nals: list[tuple[int, int, int]] = []
    for idx, (off, sc_len) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else n
        header_off = off + sc_len
        if header_off >= n or header_off >= end:
            continue
        nals.append((off, end, data[header_off]))
    return nals


def _nal_type(header: int, codec: str) -> int:
    if codec == "h264":
        return header & 0x1F
    return (header >> 1) & 0x3F


def detect_codec(data: bytes) -> Optional[str]:
    """从码流里识别是 H.264 还是 H.265，判断不了返回 None。"""
    h264_score = 0
    h265_score = 0
    for _, _, header in split_nals(data):
        if header & 0x80:
            continue  # forbidden_zero_bit 置位，非法 NAL
        if (header & 0x1F) in (_H264_SPS, _H264_PPS, _H264_IDR, _H264_NON_IDR):
            h264_score += 1
        if ((header >> 1) & 0x3F) in _H265_PARAM_OR_IRAP:
            h265_score += 1
    if h264_score > h265_score:
        return "h264"
    if h265_score > h264_score:
        return "hevc"
    return None


def is_keyframe(data: bytes, codec: str) -> bool:
    """该访问单元是否含 IDR / IRAP 关键帧。"""
    for _, _, header in split_nals(data):
        nal_type = _nal_type(header, codec)
        if codec == "h264":
            if nal_type == _H264_IDR:
                return True
        elif nal_type in _H265_IRAP:
            return True
    return False


def collect_param_sets(data: bytes, codec: str) -> Optional[bytes]:
    """取出本帧里的参数集（VPS/SPS/PPS），按解码器需要的顺序排列。

    码流里不一定每个关键帧都带参数集，所以每次出现都要缓存最新的一份，
    新开一段时补在段首，保证 ffmpeg 能解析出编码参数。
    """
    found: dict[int, bytes] = {}
    for start, end, header in split_nals(data):
        nal_type = _nal_type(header, codec)
        if codec == "h264":
            if nal_type in (_H264_SPS, _H264_PPS):
                found.setdefault(nal_type, data[start:end])
        else:
            if nal_type in (_H265_VPS, _H265_SPS, _H265_PPS):
                found.setdefault(nal_type, data[start:end])
    if not found:
        return None
    order = (7, 8) if codec == "h264" else (_H265_VPS, _H265_SPS, _H265_PPS)
    return b"".join(found[key] for key in order if key in found)


@dataclass
class SegmentSettings:
    """开新段时读取的一次性配置快照。"""

    storage_root: str
    folder: str
    segment_minutes: int
    input_fps: int
    min_free_gb: int


class SegmentWriter(threading.Thread):
    """单路相机的落盘线程：帧入队，关键帧切段，ffmpeg 封装。"""

    def __init__(
        self,
        did: str,
        get_settings: Callable[[], SegmentSettings],
        ffmpeg_log: Optional[Path] = None,
    ) -> None:
        super().__init__(name=f"writer-{did[-6:]}", daemon=True)
        self.did = did
        self._get_settings = get_settings
        self._queue: queue.Queue[Optional[tuple[bytes, int]]] = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop_event = threading.Event()

        self._codec: Optional[str] = None
        self._param_sets = b""
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._segment_started = 0.0
        self._segment_bytes = 0

        # 帧率实测：相机 ts 单位不定（ms/us/ns），靠候选换算取落在合理区间的那个。
        self._ts_intervals: deque[int] = deque(maxlen=_FPS_WINDOW)
        self._last_ts = 0
        self._first_frame_at = 0.0

        self._lock = threading.Lock()
        self.frames = 0
        self.dropped = 0
        self.bytes_written = 0
        self.segments = 0
        self.ffmpeg_failures = 0
        self.paused_reason = ""
        self.last_write_ts = 0.0

        self._ffmpeg_log_path = ffmpeg_log
        self._ffmpeg_log_fp = None

    # ── 对外接口 ──────────────────────────────────────────────
    def push(self, data: bytes, ts: int = 0) -> None:
        """接收一帧原始码流；队列满时丢弃并计数，绝不阻塞事件循环。"""
        if self._stop_event.is_set():
            return
        try:
            self._queue.put_nowait((data, ts or 0))
        except queue.Full:
            with self._lock:
                self.dropped += 1
            if self.dropped % 100 == 1:
                _LOGGER.warning("writer queue full, dropping frames, %s", self.did)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self.join(timeout=20.0)
        self._close_segment()

    def status(self) -> dict:
        with self._lock:
            return {
                "codec": self._codec,
                "frames": self.frames,
                "dropped": self.dropped,
                "bytes_written": self.bytes_written,
                "segments": self.segments,
                "ffmpeg_failures": self.ffmpeg_failures,
                "paused_reason": self.paused_reason,
                "current_file": str(self._path) if self._path else "",
                "segment_bytes": self._segment_bytes,
                "segment_seconds": (
                    int(time.monotonic() - self._segment_started)
                    if self._proc and self._segment_started
                    else 0
                ),
                "last_write_age": (
                    round(time.monotonic() - self.last_write_ts, 1)
                    if self.last_write_ts
                    else None
                ),
                "param_sets": bool(self._param_sets),
                "estimated_fps": (
                    round(est, 2) if (est := self._estimate_fps()) else None
                ),
            }

    # ── 线程主体 ──────────────────────────────────────────────
    def run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            data, ts = item
            try:
                self._handle(data, ts)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("segment writer failed, %s", self.did)
        self._close_segment()

    def _handle(self, data: bytes, ts: int) -> None:
        if self._first_frame_at == 0.0:
            self._first_frame_at = time.monotonic()
        self._track_ts(ts)

        if self._codec is None:
            detected = detect_codec(data)
            if detected is None:
                return
            self._codec = detected
            _LOGGER.info("codec detected, %s, %s", self.did, detected)

        param_sets = collect_param_sets(data, self._codec)
        if param_sets:
            self._param_sets = param_sets

        key = is_keyframe(data, self._codec)
        with self._lock:
            self.frames += 1

        if self._proc is None:
            if not key:
                return
            if not self._fps_ready():
                # 帧率样本不足，先不建段：否则首段会按默认帧率计时。
                return
            if not self._open_segment():
                return
            self._write(self._param_sets)
            self._write(data)
            return

        settings = self._get_settings()
        if key and time.monotonic() - self._segment_started >= settings.segment_minutes * 60:
            self._close_segment()
            if not self._open_segment():
                return
            self._write(self._param_sets)
            self._write(data)
            return

        self._write(data)

    # ── 帧率估算 ──────────────────────────────────────────────
    def _track_ts(self, ts: int) -> None:
        if ts <= 0:
            return
        last = self._last_ts
        self._last_ts = ts
        if last <= 0:
            return
        delta = ts - last
        if delta <= 0:
            # 时间戳回绕或设备重置，旧样本作废。
            self._ts_intervals.clear()
            return
        self._ts_intervals.append(delta)

    def _estimate_fps(self) -> Optional[float]:
        if len(self._ts_intervals) < 10:
            return None
        values = sorted(self._ts_intervals)
        # 去掉两端 5%，抑制重连/抖动造成的异常间隔。
        cut = max(1, len(values) // 20)
        core = values[cut:-cut] or values
        mean = sum(core) / len(core)
        if mean <= 0:
            return None
        # ts 单位未知：依次假设 秒 / 毫秒 / 微秒 / 纳秒，取第一个落在合理区间的。
        for scale in (1.0, 1000.0, 1e6, 1e9):
            fps = scale / mean
            if _FPS_MIN <= fps <= _FPS_MAX:
                return fps
        return None

    def _fps_ready(self) -> bool:
        if self._get_settings().input_fps > 0:
            return True
        if self._estimate_fps() is not None:
            return True
        return time.monotonic() - self._first_frame_at >= _FPS_WARMUP_SECONDS

    def _effective_fps(self) -> float:
        override = self._get_settings().input_fps
        if override > 0:
            return float(override)
        estimate = self._estimate_fps()
        if estimate is not None:
            return round(estimate, 3)
        return _FPS_FALLBACK

    # ── 段管理 ────────────────────────────────────────────────
    def _disk_ok(self, root: Path) -> bool:
        settings = self._get_settings()
        try:
            usage = shutil.disk_usage(root)
        except OSError as exc:
            _LOGGER.warning("disk usage probe failed, %s, %s", root, exc)
            return True
        free_gb = usage.free / (1024 ** 3)
        if free_gb < settings.min_free_gb:
            with self._lock:
                self.paused_reason = f"磁盘剩余 {free_gb:.1f}GB，低于下限 {settings.min_free_gb}GB"
            return False
        with self._lock:
            self.paused_reason = ""
        return True

    def _open_segment(self) -> bool:
        settings = self._get_settings()
        root = Path(settings.storage_root)
        root.mkdir(parents=True, exist_ok=True)
        if not self._disk_ok(root):
            return False

        now = datetime.now()
        day_dir = root / settings.folder / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{now.strftime('%H%M%S')}.mp4"
        suffix = 1
        while path.exists():
            path = day_dir / f"{now.strftime('%H%M%S')}_{suffix}.mp4"
            suffix += 1

        fps = self._effective_fps()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            self._codec or "h264",
            "-framerate",
            f"{fps:.3f}",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-flush_packets",
            "1",
            # 用分片 MP4：部分相机的码流缺少 VUI 计时信息，原始 HEVC 解复用给不出
            # PTS，MKV 会直接拒绝写入；mov 封装会自己按帧率补时间戳。
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "-y",
            str(path),
        ]

        stderr = subprocess.DEVNULL
        if self._ffmpeg_log_path is not None:
            try:
                self._ffmpeg_log_path.parent.mkdir(parents=True, exist_ok=True)
                if self._ffmpeg_log_fp is None or self._ffmpeg_log_fp.closed:
                    self._ffmpeg_log_fp = open(self._ffmpeg_log_path, "ab")
                stderr = self._ffmpeg_log_fp
            except OSError:
                stderr = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        except OSError as exc:
            _LOGGER.error("spawn ffmpeg failed, %s, %s", self.did, exc)
            with self._lock:
                self.ffmpeg_failures += 1
            return False

        self._proc = proc
        self._path = path
        self._segment_started = time.monotonic()
        self._segment_bytes = 0
        with self._lock:
            self.segments += 1
            self.paused_reason = ""
        _LOGGER.info("segment opened, %s, %s, fps=%.2f", self.did, path, fps)
        return True

    def _write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        if proc.poll() is not None:
            # ffmpeg 自己退出了：这帧写不进去，等下一个关键帧重开一段。
            self._note_ffmpeg_exit(proc)
            return
        try:
            proc.stdin.write(data)
            self.last_write_ts = time.monotonic()
            with self._lock:
                self._segment_bytes += len(data)
                self.bytes_written += len(data)
        except (BrokenPipeError, ValueError, OSError):
            self._note_ffmpeg_exit(proc)

    def _note_ffmpeg_exit(self, proc: subprocess.Popen) -> None:
        code = proc.poll()
        _LOGGER.error("ffmpeg exited unexpectedly, %s, rc=%s", self.did, code)
        with self._lock:
            self.ffmpeg_failures += 1
        self._proc = None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

    def _close_segment(self) -> None:
        proc, path = self._proc, self._path
        self._proc = None
        self._path = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            rc = -1
        size = 0
        if path is not None:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        _LOGGER.info("segment closed, %s, %s, %.1fMB, rc=%s", self.did, path, size / 1e6, rc)
