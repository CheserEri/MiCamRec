"""配置与凭据持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# 与小米后端保持同一个回调地址，授权完成后由小米页面展示 code/state。
REDIRECT_URI = "https://mico.api.mijia.tech/login_redirect"
CLOUD_SERVER = "cn"


def app_dir() -> Path:
    """应用目录，可用 MICAMREC_HOME 覆盖。"""
    raw = os.environ.get("MICAMREC_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".micamrec"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def auth_path() -> Path:
    return app_dir() / "auth.json"


def socket_path() -> Path:
    return app_dir() / "control.sock"


def log_path() -> Path:
    return app_dir() / "recorder.log"


def sdk_cache_dir() -> Path:
    path = app_dir() / "sdk-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


_ILLEGAL_CHARS = set('/\\:*?"<>|')


def sanitize_name(text: str, fallback: str = "camera") -> str:
    """把摄像头名字变成可用的目录名。"""
    cleaned = "".join(
        "_" if (ch in _ILLEGAL_CHARS or ch.isspace()) else ch for ch in (text or "")
    ).strip("._-")
    return cleaned[:40] or fallback


def camera_folder(did: str, name: str = "") -> str:
    """每路相机的落盘目录名，保证唯一（带完整 did）。"""
    return f"{sanitize_name(name)}_{did}"


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class CameraConfig:
    """单路相机的可调参数。"""

    name: str = ""
    enabled: bool = False
    # None 表示跟随全局 retention_days。
    retention_days: Optional[int] = None
    # low / high；空串表示跟随全局 quality。实测部分机型（如 moc001）用 low
    # 请求时 PPCS 能连上但收不到码流，默认走 high。
    quality: str = ""


@dataclass
class Config:
    """全局配置。"""

    storage_root: str = str(Path.home() / "micamrec-record")
    segment_minutes: int = 30
    retention_days: int = 7
    min_free_gb: int = 10
    quality: str = "high"
    input_fps: int = 0
    cameras: dict[str, CameraConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cameras = {}
        for did, raw in (data.get("cameras") or {}).items():
            if isinstance(raw, dict):
                cameras[did] = CameraConfig(
                    name=str(raw.get("name") or ""),
                    enabled=bool(raw.get("enabled", False)),
                    retention_days=(
                        int(raw["retention_days"])
                        if raw.get("retention_days") is not None
                        else None
                    ),
                    quality=str(raw.get("quality") or ""),
                )
        return cls(
            storage_root=str(data.get("storage_root") or str(Path.home() / "micamrec-record")),
            segment_minutes=max(1, int(data.get("segment_minutes", 30))),
            retention_days=max(1, int(data.get("retention_days", 7))),
            min_free_gb=max(0, int(data.get("min_free_gb", 10))),
            quality=str(data.get("quality") or "high"),
            input_fps=max(0, int(data.get("input_fps", 0))),
            cameras=cameras,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_root": self.storage_root,
            "segment_minutes": self.segment_minutes,
            "retention_days": self.retention_days,
            "min_free_gb": self.min_free_gb,
            "quality": self.quality,
            "input_fps": self.input_fps,
            "cameras": {did: asdict(cfg) for did, cfg in self.cameras.items()},
        }

    def camera(self, did: str) -> CameraConfig:
        return self.cameras.setdefault(did, CameraConfig())

    def retention_for(self, did: str) -> int:
        cfg = self.cameras.get(did)
        if cfg and cfg.retention_days is not None:
            return max(1, int(cfg.retention_days))
        return max(1, int(self.retention_days))


class ConfigStore:
    """config.json 的读写。"""

    def load(self) -> Config:
        path = config_path()
        if not path.exists():
            cfg = Config()
            self.save(cfg)
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return Config.from_dict(data)

    def save(self, cfg: Config) -> None:
        _atomic_write(
            config_path(),
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
            mode=0o600,
        )


@dataclass
class AuthState:
    """auth.json 的结构。"""

    uuid: str
    redirect_uri: str = REDIRECT_URI
    cloud_server: str = CLOUD_SERVER
    oauth: Optional[dict[str, Any]] = None
    updated_ts: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthState":
        return cls(
            uuid=str(data.get("uuid") or uuid.uuid4().hex),
            redirect_uri=str(data.get("redirect_uri") or REDIRECT_URI),
            cloud_server=str(data.get("cloud_server") or CLOUD_SERVER),
            oauth=data.get("oauth"),
            updated_ts=int(data.get("updated_ts") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuthStore:
    """auth.json 的读写，权限 0600。"""

    def load(self) -> AuthState:
        path = auth_path()
        if path.exists():
            try:
                return AuthState.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        state = AuthState(uuid=uuid.uuid4().hex)
        self.save(state)
        return state

    def save(self, state: AuthState) -> None:
        _atomic_write(
            auth_path(),
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            mode=0o600,
        )

    def save_oauth(self, oauth_info: Any) -> None:
        state = self.load()
        if hasattr(oauth_info, "model_dump"):
            state.oauth = oauth_info.model_dump(mode="json")
        else:
            state.oauth = dict(oauth_info)
        import time

        state.updated_ts = int(time.time())
        self.save(state)

    def clear_oauth(self) -> None:
        state = self.load()
        state.oauth = None
        state.updated_ts = 0
        self.save(state)
