# 开源许可与第三方组件清单

更新日期：2026-10-16。本清单针对 `requirements-linux-mvp.txt` 与当前 Ubuntu
运行链路；具体许可证文本以各组件随附发行包为准。

## 项目与上游

- 本仓库基于 `LC044/WeChatMsg`，保留上游作者、贡献者署名和根目录
  `LICENSE`，整体按 GNU GPL v3.0 分发。
- 当前派生仓库为 `chenanhan230-del/WeChatMsg-4x`。详细修改关系见
  `doc/修改声明.md`；本分支新增的 `wx4/`、`tools/` 和 `docs/` 代码沿用
  GPL-3.0。
- 分发源代码或二进制时必须同时保留 `LICENSE`、`doc/修改声明.md`、本清单和
  对应源码获取方式，不得删除上游署名。

## Python 直接依赖

| 组件 | 当前验证版本 | 许可证 | 用途 |
|---|---:|---|---|
| PyCryptodomeX | 3.24.1 | BSD / Public Domain | SQLCipher 页面 AES/HMAC 处理 |
| python-zstandard | 0.25.0 | BSD-3-Clause | 微信 4.x 消息正文解压 |
| pilk | 0.2.4 | GPL-3.0 | SILK 语音解码 |
| Pillow | 12.3.0 | MIT-CMU | 图片解码、缩略图与动画验证 |
| Flask | 3.1.3 | BSD-3-Clause | 本地 HTTP 服务 |

## Flask 运行时依赖

| 组件 | 当前验证版本 | 许可证 |
|---|---:|---|
| Werkzeug | 3.1.9 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| Click | 8.5.0 | BSD-3-Clause |
| Blinker | 1.9.0 | MIT |
| ItsDangerous | 2.2.0 | BSD |

## 系统组件

- FFmpeg：用于 HEVC/WXGF 图片与 SILK 音频转码。当前实机为 Ubuntu
  `ffmpeg 6.1.1-3ubuntu5`，构建参数启用了 GPL 组件。FFmpeg 及其编解码库由
  Ubuntu 系统包提供，本仓库不捆绑其二进制。
- SQLite/FTS5：由 Python 标准库与 Ubuntu 运行时提供，用于统一档案、全文搜索
  和一致性检查。
- 浏览器 HTML5 音视频能力：运行时使用，不随项目再分发浏览器代码。

## 维护要求

依赖升级时必须重新读取安装包元数据和许可证文件；不能只沿用本表中的版本与
许可结论。若未来打包 FFmpeg、浏览器、字体或本地模型，需要把对应完整许可、
NOTICE、源码提供义务和模型条款加入分发物。
