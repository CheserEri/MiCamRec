# MiCamRec

登录小米账号，把小米摄像头的原始码流录制到本地磁盘的轻量服务：命令行 + 终端面板 + systemd 常驻，只做录像，不转码、不依赖云存储。

## 特性

- **零转码**：直接 `-c copy` 封装 SDK 回调的 Annex-B 码流，CPU 占用极低
- **分片 MP4**：按关键帧切段，`+frag_keyframe+empty_moov` 写入，进程被杀/断电也能正常播放
- **按摄像头 + 日期分目录**，文件名即开始时间
- **可调策略**：分段时长、保留天数（全局/单路）、画质、最低剩余空间
- **帧率实测**：相机回调时间戳单位不统一（ms / 90kHz / uint64 回绕），按候选换算取合理值
- **终端面板**：切换录像、改保留天数/分段/存储目录，守护进程由 systemd 常驻
- **开机自启**：systemd 用户服务 + `loginctl enable-linger`，崩溃自动重启

## 限制（使用前请确认）

1. 依赖小米闭源库 `libmiot_camera_lite.so`，其 PPCS 初始化参数写死了 `SkipTCPRelay=1`、`SkipUDPRelay=1`，**只支持局域网直连或可直连的 P2P**。跨网段/4G/CGNAT 后的摄像头会在 `PPCS_Connect errorcode: -3` 处失败，无法通过任何参数放开。
2. 不支持 RTSP / ONVIF，小米消费级摄像头不开放标准协议。
3. 需要先用小米账号完成一次 OAuth 授权，凭据保存在本机 `~/.micamrec/auth.json`。

## 环境要求

- Linux（x86_64 / arm64），Python ≥ 3.11
- ffmpeg 可执行文件在 PATH 中
- 服务器与摄像头在同一网段（或在可直连的 P2P 范围内）

## 安装

```bash
git clone https://github.com/CheserEri/MiCamRec.git
cd MiCamRec
python3 -m venv .venv
.venv/bin/pip install ./backend/miot ./recorder
.venv/bin/micam-rec --help
```

建议加个软链，方便直接调用：

```bash
ln -sf "$PWD/.venv/bin/micam-rec" ~/.local/bin/micam-rec
```

## 登录与开录

```bash
micam-rec login          # 打开给出的链接，把回调页的授权码粘回来
micam-rec cameras        # 查看摄像头与局域网/在线状态
micam-rec enable 1       # 按序号、名称或 DID 开录
micam-rec panel          # 打开终端面板（会自动拉起守护进程）
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `micam-rec login` / `logout` | 小米账号授权 / 清除凭据 |
| `micam-rec cameras` | 列出摄像头（序号、DID、局域网、在线） |
| `micam-rec enable <序号\|名称\|DID>` | 开始录制某一路 |
| `micam-rec disable <...>` | 停止录制某一路 |
| `micam-rec status [--json]` | 守护进程、写入量、当前段等状态 |
| `micam-rec panel` | curses 面板 |
| `micam-rec serve` | 前台运行守护进程（调试用） |
| `micam-rec retention <天数>` | 全局保留天数 |
| `micam-rec segment <分钟>` | 分段时长 |
| `micam-rec quality high\|low` | 画质 |
| `micam-rec disk` | 各摄像头占用与剩余空间 |
| `micam-rec stop` | 停止守护进程 |

面板快捷键：`空格` 开关录像、`R` 保留天数、`S` 分段、`D` 最低剩余空间、`G` 存储目录、`F` 刷新、`X` 停守护进程、`Q` 退出。

## 开机自启（systemd 用户服务）

`~/.config/systemd/user/micamrec.service`：

```ini
[Unit]
Description=MiCamRec camera recorder daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/MiCamRec/.venv/bin/micam-rec serve
Restart=always
RestartSec=10
WorkingDirectory=%h/MiCamRec
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now micamrec.service
loginctl enable-linger "$USER"   # 不登录也常驻
journalctl --user -u micamrec -f
```

## 配置

配置文件 `~/.micamrec/config.json`（目录可用环境变量 `MICAMREC_HOME` 覆盖），面板/CLI 修改后立即落盘。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `storage_root` | `~/micamrec-record` | 录像根目录 |
| `segment_minutes` | `30` | 单段时长（关键帧处切） |
| `retention_days` | `7` | 超期删除 |
| `min_free_gb` | `2` | 剩余空间低于该值暂停写入 |
| `quality` | `high` | 全局画质；单路可留空跟随全局 |
| `input_fps` | `0` | 0 表示自动实测帧率 |

目录结构：`<storage_root>/<摄像头名>_<DID>/<YYYY-MM-DD>/<HHMMSS>.mp4`

## 容量参考

实测 2560×1440 / 1920×2160 HEVC 每路 30 分钟约 330–370 MB，即 **约 16–17 GB/天/路**，两路 7 天约 230–240 GB。夜视或画面噪点多时码率会上浮。

## 目录结构

```
recorder/                 # MiCamRec 本体
  pyproject.toml          # 入口 micam-rec -> micamrec.cli:main
  src/micamrec/
    auth.py               # OAuth 与凭据
    config.py             # 配置/路径
    engine.py             # 守护进程、相机生命周期、清理
    segmenter.py          # 原始码流切段 + ffmpeg 封装
    control.py            # Unix socket 控制协议
    tui.py                # curses 面板
    cli.py                # 命令入口
backend/miot/             # 小米 MIoT SDK（含闭源 PPCS 库）
```

## 许可

`backend/miot` 及 `libmiot_camera_lite.*` 来自 Xiaomi Miloco 项目，见 [LICENSE.md](LICENSE.md) 与 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。`recorder/` 为独立实现。
