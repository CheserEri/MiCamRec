"""小米账号 OAuth 登录、令牌续期与 MIoTClient 构建。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from miot.client import MIoTClient

from .config import AuthStore, sdk_cache_dir

_LOGGER = logging.getLogger(__name__)

# 小米的 access_token 有效期内一般不需要频繁续期；提前 120 秒刷新，避免录制
# 中途恰好过期导致建流失败。
_TOKEN_LEAD_SECONDS = 120


def logged_in(store: AuthStore) -> bool:
    state = store.load()
    return bool(state.oauth and state.oauth.get("access_token"))


def build_client_sync_args(store: AuthStore) -> dict[str, Any]:
    """构造 MIoTClient 需要的参数（不发起网络请求）。"""
    state = store.load()
    return {
        "uuid": state.uuid,
        "redirect_uri": state.redirect_uri,
        "cache_path": str(sdk_cache_dir()),
        "oauth_info": state.oauth,
        "cloud_server": state.cloud_server,
        "central_hub_enabled": False,
    }


def new_client(store: AuthStore) -> MIoTClient:
    """创建 MIoTClient 实例，调用方负责 await init_async()。"""
    return MIoTClient(**build_client_sync_args(store))


async def build_client(store: AuthStore) -> MIoTClient:
    """构建并初始化 MIoTClient。"""
    client = new_client(store)
    await client.init_async()
    return client


async def make_oauth_url(store: AuthStore) -> tuple[MIoTClient, str]:
    """创建 client 并生成小米授权 URL。

    返回值里的 client 必须用于同一次登录流程的 complete_login（state 只在
    该实例内存里，换实例无法校验）。
    """
    client = new_client(store)
    await client.init_async()
    url = await client.gen_oauth_url_async(store.load().redirect_uri)
    return client, url


def parse_oauth_payload(text: str) -> tuple[str, str]:
    """解析回调页面提供的授权信息。

    支持三种粘贴形式：
    1. 回调页面的 base64 串（base64({"code":..., "state":...})）；
    2. 完整回调 URL（https://...?code=...&state=...）；
    3. 裸查询串（code=...&state=...）。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("授权信息为空")

    if "code=" in raw and "state=" in raw:
        query = urlparse(raw).query if raw.startswith("http") else raw
        params = parse_qs(query)
        code = (params.get("code") or [""])[0].strip()
        state = (params.get("state") or [""])[0].strip()
        if code and state:
            return code, state
        raise ValueError("链接里缺少 code 或 state")

    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        data = json.loads(decoded)
        code = str(data["code"]).strip()
        state = str(data["state"]).strip()
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("授权信息格式错误，请从回调页面直接复制") from exc
    if not code or not state:
        raise ValueError("授权信息中 code 或 state 为空")
    return code, state


async def complete_login(
    client: MIoTClient, store: AuthStore, payload: str
) -> None:
    """用回调信息换取令牌并落盘。"""
    code, state = parse_oauth_payload(payload)
    await client.get_access_token_async(code=code, state=state, persist=store.save_oauth)


async def ensure_token(client: MIoTClient, store: AuthStore) -> bool:
    """令牌临近过期时刷新；返回是否仍然可用。"""
    state = store.load()
    oauth = state.oauth or {}
    if not oauth.get("access_token"):
        return False
    expires_ts = int(oauth.get("expires_ts") or 0)
    if expires_ts - _TOKEN_LEAD_SECONDS > time.time():
        return True
    refresh_token = oauth.get("refresh_token")
    if not refresh_token:
        _LOGGER.error("access token expired and no refresh token")
        return False
    try:
        await client.refresh_access_token_async(
            refresh_token=refresh_token, persist=store.save_oauth
        )
        _LOGGER.info("access token refreshed")
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("refresh access token failed: %s", exc)
        return False
