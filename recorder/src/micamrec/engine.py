"""录制守护进程：登录态维护、相机开关、保留清理与状态汇总。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from miot.types import MIoTCameraStatus, MIoTCameraVideoQuality

from . import auth
from .config import AuthStore, ConfigStore, app_dir, camera_folder
from .control import ControlServer
from .segmenter import SegmentSettings, SegmentWriter

_LOGGER = logging.getLogger(__name__)

_STATUS_TEXT = {
    MIoTCameraStatus.DISCONNECTED: "未连接",
    MIoTCameraStatus.CONNECTING: "连接中",
    MIoTCameraStatus.RE_CONNECTING: "重连中",
    MIoTCameraStatus.CONNECTED: "已连接",
    MIoTCameraStatus.ERROR: "错误",
}

_QUALITY_MAP = {
    "low": MIoTCameraVideoQuality.LOW,
    "high": MIoTCameraVideoQuality.HIGH,
}


class CameraRunner:
    """单路相机的拉流 + 落盘。"""

    def __init__(self, engine: "Engine", did: str) -> None:
        self.engine = engine
        self.did = did
        self.instance = None
        self.writer: Optional[SegmentWriter] = None
        self.connection = MIoTCameraStatus.DISCONNECTED
        self.last_error = ""
        self.raw_frames = 0

    async def start(self) -> None:
        client = self.engine.client
        info = self.engine.cameras.get(self.did)
        if client is None or info is None:
            raise RuntimeError("相机不存在或未登录")
        cfg = self.engine.cfg
        camera_cfg = cfg.camera(self.did)
        quality = _QUALITY_MAP.get(camera_cfg.quality or cfg.quality, MIoTCameraVideoQuality.LOW)

        self.instance = await client.create_camera_instance_async(
            info, frame_interval=500, enable_hw_accel=False
        )
        await self.instance.register_status_changed_async(self._on_status)
        await self.instance.start_async(
            qualities=quality,
            enable_audio=False,
            enable_reconnect=True,
        )

        self.writer = SegmentWriter(
            did=self.did,
            get_settings=self._segment_settings,
            ffmpeg_log=app_dir() / f"ffmpeg-{self.did[-6:]}.log",
        )
        self.writer.start()
        await self.instance.register_raw_video_async(self._on_raw_video, channel=0)
        _LOGGER.info("camera runner started, %s", self.did)

    async def stop(self) -> None:
        instance = self.instance
        self.instance = None
        if instance is not None:
            try:
                await instance.unregister_raw_video_async(channel=0)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("unregister raw video failed, %s, %s", self.did, exc)
            try:
                await instance.stop_async()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("camera stop failed, %s, %s", self.did, exc)
        if self.writer is not None:
            writer, self.writer = self.writer, None
            await asyncio.to_thread(writer.stop)
        camera_client = getattr(self.engine.client, "camera_client", None)
        if camera_client is not None:
            try:
                await camera_client.destroy_camera_async(self.did)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("destroy camera failed, %s, %s", self.did, exc)
        _LOGGER.info("camera runner stopped, %s", self.did)

    def _segment_settings(self) -> SegmentSettings:
        cfg = self.engine.cfg
        folder = camera_folder(self.did, cfg.camera(self.did).name)
        return SegmentSettings(
            storage_root=cfg.storage_root,
            folder=folder,
            segment_minutes=max(1, cfg.segment_minutes),
            input_fps=max(0, cfg.input_fps),
            min_free_gb=max(0, cfg.min_free_gb),
        )

    async def _on_raw_video(self, did: str, data: bytes, ts: int, seq: int, channel: int) -> None:
        self.raw_frames += 1
        if self.raw_frames <= 3:
            _LOGGER.info(
                "raw frame arrived, %s, len=%d, ts=%d, head=%s",
                did,
                len(data),
                ts,
                data[:8].hex(),
            )
        writer = self.writer
        if writer is not None:
            writer.push(data, ts)

    async def _on_status(self, did: str, status: MIoTCameraStatus) -> None:
        self.connection = status

    def status(self) -> dict[str, Any]:
        writer = self.writer
        return {
            "did": self.did,
            "recording": writer is not None,
            "connection": _STATUS_TEXT.get(self.connection, str(self.connection)),
            "error": self.last_error,
            "raw_frames": self.raw_frames,
            "writer": writer.status() if writer else None,
        }


class Engine:
    """守护进程主体，控制面板通过 Unix socket 读写它的状态。"""

    def __init__(self) -> None:
        self.cfg_store = ConfigStore()
        self.auth_store = AuthStore()
        self.cfg = self.cfg_store.load()
        self.client = None
        self.cameras: dict[str, Any] = {}
        self.runners: dict[str, CameraRunner] = {}
        self.state = "starting"
        self.last_error = ""
        self.started_ts = int(time.time())
        self.control = ControlServer(self)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._build_lock = asyncio.Lock()

    # ── 生命周期 ──────────────────────────────────────────────
    async def run(self) -> None:
        Path(self.cfg.storage_root).expanduser().mkdir(parents=True, exist_ok=True)
        await self.control.start()
        self._tasks = [
            asyncio.create_task(self._client_loop(), name="client-loop"),
            asyncio.create_task(self._token_loop(), name="token-loop"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup-loop"),
        ]
        _LOGGER.info("recorder engine started, pid=%s", os.getpid())
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        runners = list(self.runners.values())
        self.runners.clear()
        if runners:
            await asyncio.gather(*(r.stop() for r in runners), return_exceptions=True)
        await self.control.stop()
        if self.client is not None:
            client, self.client = self.client, None
            try:
                await client.deinit_async()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("client deinit failed: %s", exc)
        _LOGGER.info("recorder engine stopped")

    def request_stop(self) -> None:
        self._stop.set()

    # ── 后台任务 ──────────────────────────────────────────────
    async def _client_loop(self) -> None:
        while not self._stop.is_set():
            if self.client is None:
                if auth.logged_in(self.auth_store):
                    await self._build_client()
                else:
                    self.state = "not_logged_in"
                    self.last_error = "尚未登录小米账号，请先运行 micam-rec login"
            await self._sleep(30)

    async def _build_client(self) -> None:
        if self._build_lock.locked():
            return
        async with self._build_lock:
            self.state = "starting"
            try:
                client = await auth.build_client(self.auth_store)
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.last_error = f"初始化 MIoT 客户端失败: {exc}"
                _LOGGER.exception("build miot client failed")
                return
            self.client = client
            self.last_error = ""
            try:
                await self.refresh_cameras()
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.last_error = f"拉取摄像头失败: {exc}"
                _LOGGER.exception("fetch cameras failed")
                return
            await self._reconcile()
            if self.state != "error":
                self.state = "running"
            _LOGGER.info("miot client ready, cameras=%d", len(self.cameras))

    async def _token_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(600)
            client = self.client
            if client is None:
                continue
            if not await auth.ensure_token(client, self.auth_store):
                self.last_error = "访问令牌续期失败，可能需要重新登录"
                _LOGGER.error("token refresh failed")

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                deleted = await asyncio.to_thread(self.cleanup)
                if deleted:
                    _LOGGER.info("retention cleanup removed %d dirs", deleted)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("retention cleanup failed")
            await self._sleep(3600)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ── 相机管理 ──────────────────────────────────────────────
    async def refresh_cameras(self) -> None:
        client = self.client
        if client is None:
            return
        cameras = await client.get_cameras_async()
        self.cameras = dict(cameras)
        changed = False
        for did, info in self.cameras.items():
            cam_cfg = self.cfg.cameras.get(did)
            if cam_cfg is None:
                self.cfg.camera(did).name = info.name
                changed = True
            elif cam_cfg.name != info.name:
                cam_cfg.name = info.name
                changed = True
        if changed:
            self.cfg_store.save(self.cfg)

    async def _reconcile(self) -> None:
        for did, cam_cfg in list(self.cfg.cameras.items()):
            if cam_cfg.enabled and did in self.cameras and did not in self.runners:
                await self._start_runner(did)
        for did in list(self.runners):
            cam_cfg = self.cfg.cameras.get(did)
            if cam_cfg is None or not cam_cfg.enabled or did not in self.cameras:
                await self._stop_runner(did)

    async def _start_runner(self, did: str) -> bool:
        runner = CameraRunner(self, did)
        try:
            await runner.start()
        except Exception as exc:  # noqa: BLE001
            runner.last_error = str(exc)
            _LOGGER.exception("start camera failed, %s", did)
            try:
                await runner.stop()
            except Exception:  # noqa: BLE001
                pass
            self.last_error = f"启动相机失败 {did}: {exc}"
            return False
        self.runners[did] = runner
        return True

    async def _stop_runner(self, did: str) -> None:
        runner = self.runners.pop(did, None)
        if runner is not None:
            await runner.stop()

    async def enable(self, did: str) -> dict[str, Any]:
        cam_cfg = self.cfg.camera(did)
        cam_cfg.enabled = True
        self.cfg_store.save(self.cfg)
        if self.client is not None and did in self.cameras and did not in self.runners:
            ok = await self._start_runner(did)
            if not ok:
                return {"ok": False, "error": "启动相机失败，详见守护进程日志"}
        return {"ok": True}

    async def disable(self, did: str) -> dict[str, Any]:
        cam_cfg = self.cfg.camera(did)
        cam_cfg.enabled = False
        self.cfg_store.save(self.cfg)
        await self._stop_runner(did)
        return {"ok": True}

    # ── 保留清理 ──────────────────────────────────────────────
    def cleanup(self) -> int:
        """按摄像头 + 日期删除超过保留期的目录，返回删除的日期目录数。"""
        root = Path(self.cfg.storage_root).expanduser()
        if not root.exists():
            return 0
        removed = 0
        today = date.today()
        for did, cam_cfg in self.cfg.cameras.items():
            retention = self.cfg.retention_for(did)
            cutoff = today - timedelta(days=retention)
            cam_dir = root / camera_folder(did, cam_cfg.name)
            if not cam_dir.exists():
                continue
            for day_dir in cam_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    removed += 1
                    _LOGGER.info("retention: removed %s (retention=%d)", day_dir, retention)
            if cam_dir.exists() and not any(cam_dir.iterdir()):
                try:
                    cam_dir.rmdir()
                except OSError:
                    pass
        return removed

    # ── 状态 / 控制 ───────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        cameras = []
        for did, info in self.cameras.items():
            cam_cfg = self.cfg.cameras.get(did)
            runner = self.runners.get(did)
            row = {
                "did": did,
                "name": (cam_cfg.name if cam_cfg and cam_cfg.name else info.name),
                "enabled": bool(cam_cfg.enabled) if cam_cfg else False,
                "recording": runner is not None,
                "connection": (
                    _STATUS_TEXT.get(runner.connection, str(runner.connection))
                    if runner
                    else _STATUS_TEXT[MIoTCameraStatus.DISCONNECTED]
                ),
                "retention_days": self.cfg.retention_for(did),
                "quality": (cam_cfg.quality if cam_cfg else self.cfg.quality),
                "writer": runner.status()["writer"] if runner else None,
                "raw_frames": runner.raw_frames if runner else 0,
                "error": runner.last_error if runner else "",
            }
            cameras.append(row)
        disk = {"free_gb": 0.0, "total_gb": 0.0}
        try:
            usage = shutil.disk_usage(Path(self.cfg.storage_root).expanduser())
            disk = {
                "free_gb": round(usage.free / (1024 ** 3), 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
            }
        except OSError:
            pass
        cfg_dump = self.cfg.to_dict()
        return {
            "ok": True,
            "state": self.state,
            "error": self.last_error,
            "pid": os.getpid(),
            "uptime": int(time.time() - self.started_ts),
            "now": int(time.time()),
            "config": cfg_dump,
            "disk": disk,
            "cameras": cameras,
        }

    async def set_values(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"storage_root", "segment_minutes", "retention_days", "min_free_gb", "input_fps", "quality"}
        applied = {}
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"不支持修改 {key}")
            if key == "storage_root":
                path = str(value).strip()
                if not path:
                    raise ValueError("存储目录不能为空")
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
                self.cfg.storage_root = path
            elif key == "quality":
                if str(value) not in _QUALITY_MAP:
                    raise ValueError("quality 只能是 low 或 high")
                self.cfg.quality = str(value)
                for cam in self.cfg.cameras.values():
                    cam.quality = ""
            else:
                number = int(value)
                if number < 1 and key != "min_free_gb" and key != "input_fps":
                    raise ValueError(f"{key} 必须大于 0")
                setattr(self.cfg, key, number)
            applied[key] = value
        self.cfg_store.save(self.cfg)
        if "retention_days" in applied:
            await asyncio.to_thread(self.cleanup)
        if "storage_root" in applied:
            # 正在写的段还指着旧目录，换路径必须立刻重开段，否则新文件仍落到旧目录。
            await self._restart_runners()
        if "quality" in applied:
            # 画质只在 start 时下发给 SDK，必须重启正在录的相机才生效。
            await self._restart_runners()
        return applied

    async def _restart_runners(self) -> None:
        dids = list(self.runners)
        for did in dids:
            await self._stop_runner(did)
        for did in dids:
            await self._start_runner(did)

    async def handle(self, cmd: str, args: dict[str, Any]) -> Any:
        if cmd == "status":
            return self.status()
        if cmd == "refresh":
            await self.refresh_cameras()
            await self._reconcile()
            return self.status()
        if cmd == "enable":
            return await self.enable(str(args["did"]))
        if cmd == "disable":
            return await self.disable(str(args["did"]))
        if cmd == "set":
            return {"ok": True, "applied": await self.set_values(args)}
        if cmd == "cleanup":
            return {"ok": True, "removed": await asyncio.to_thread(self.cleanup)}
        if cmd == "shutdown":
            asyncio.create_task(self._delayed_stop())
            return {"ok": True}
        raise ValueError(f"未知命令 {cmd}")

    async def _delayed_stop(self) -> None:
        await asyncio.sleep(0.2)
        self.request_stop()
