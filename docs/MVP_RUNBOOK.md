# Ubuntu MVP 运行手册

本手册适用于腾讯官方 Linux 微信 4.x。原始微信目录始终只读；密钥、解密库和导出文件写入被 `.gitignore` 忽略的 `mvp-output/`。

## 1. 安装最小依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-linux-mvp.txt
```

## 2. 检查微信和账号

```bash
.venv/bin/python -m wx4 --data-root /data/wechat/xwechat_files info
```

## 3. 提取密钥

先启动并登录微信。将下面的 `<account_dir>` 换成 `info` 显示的账号目录，`<wxid>` 换成去掉末尾 4 位十六进制后缀的账号 ID。

```bash
mkdir -p mvp-output/<wxid>
chmod 700 mvp-output/<wxid>
pkexec .venv/bin/python tools/linux_key_helper.py \
  --db-storage /data/wechat/xwechat_files/<account_dir>/db_storage \
  --key-file "$PWD/mvp-output/<wxid>/keys.json"
```

`pkexec` 只提升密钥辅助程序。密钥候选必须通过对应数据库第一页 HMAC-SHA512 校验才会写入文件；结果文件会归还给登录用户并设为 `0600`。

## 4. 解密快照

```bash
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <wxid> \
  --key-file mvp-output/<wxid>/keys.json \
  decrypt --out-dir mvp-output/<wxid>/decrypted
```

程序会逐库复制 DB/WAL/SHM 快照，只合并当前 WAL 世代中已提交的帧，并对解密结果执行 SQLite 完整性检查。

## 5. 列出会话和导出 HTML

```bash
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <wxid> \
  sessions --decrypted-dir mvp-output/<wxid>/decrypted

.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <wxid> \
  export --decrypted-dir mvp-output/<wxid>/decrypted \
  --export-dir mvp-output/<wxid>/export \
  --session <session_username> -f html --media

# 同一份富媒体记录也可导出为 Markdown
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <wxid> \
  export --decrypted-dir mvp-output/<wxid>/decrypted \
  --export-dir mvp-output/<wxid>/export \
  --session <session_username> -f md --media
```

MVP HTML 为完全离线导出，远程头像会回退为文字占位。
`--media` 会自动完成：

- Linux V2 图片/表情密钥派生与解密；
- wxgf/HEVC 单帧转 JPEG，缩略图点击原图；
- SILK 语音转 MP3，生成 `<audio controls>`；
- MP4 与 poster 关联，生成 `<video controls>`；
- 内置表情还原为动画 WebP；
- URL 还原为可点击卡片，并禁止非 HTTP(S) 协议。

`.dat` 或 `.silk` 只是保留源文件，不代表富媒体验收通过。
每个会话的媒体目录下会生成 `media-manifest.json`，记录成功数量和
未下载、元数据缺失、解密失败、转码失败等问题。

## 6. 验证富媒体 HTML / Markdown

```bash
.venv/bin/python tools/validate_rich_export.py \
  mvp-output/<wxid>/export/<conversation>.html \
  --require images=1 \
  --require image_links=1 \
  --require audio_players=1 \
  --require video_players=1 \
  --require emojis=1
```

验收器会检查所有本地路径、图片/动画解码、音视频 codec 与时长、
HTML `controls` 以及 URL 的安全点击属性。某个会话本身没有某类消息时，
不要对该会话强制对应的 `--require`。

Markdown 使用同一个验收器；其中的 HTML5 音视频标签、图片缩略图、原图链接
和动态表情也会被实际解析与解码：

```bash
.venv/bin/python tools/validate_rich_export.py \
  mvp-output/archive/exports/<任务>/<会话>.md \
  --require images=1 --require image_links=1 \
  --require audio_players=1 --require video_players=1
```

## 7. 建立统一档案库

```bash
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <wxid> \
  archive --decrypted-dir mvp-output/<wxid>/decrypted \
  --archive-db mvp-output/archive/archive.sqlite3 \
  --archive-media-dir mvp-output/archive/media \
  --media

# 中文全文搜索
.venv/bin/python -m wx4 archive-search "语音" \
  --archive-db mvp-output/archive/archive.sqlite3 \
  --account <wxid>
```

再次运行同一条 `archive` 命令时，程序会对已解密源库的 mtime、
大小和 SHA-256 进行对比。源快照未变时会直接跳过消息扫描；
变化时使用 server_id/稳定复合键原位更新，不会重复插入。

## 8. 启动本地知识库界面

```bash
.venv/bin/python -m wx4 web \
  --archive-db mvp-output/archive/archive.sqlite3 \
  --setup-root mvp-output \
  --port 8765
```

终端会打印一条包含一次性随机访问令牌的完整地址。用该地址打开页面；
令牌会保存在当前标签页的会话存储中，地址栏随后自动隐藏令牌。服务仅监听
`127.0.0.1`，不会向局域网或互联网开放。关闭终端中的进程即可停止服务。

界面包含档案概览、账号切换、会话列表、全文搜索、消息类型/月份/日期过滤、
联系人和群聊、媒体画廊、会话详情，以及可直接查看或播放的图片、语音、
视频、表情和文件。年度报告在本机生成日历热力图、月/小时分布、收发占比、
高频会话、群成员排行和词频，不请求外部分析服务。

导出中心支持单会话或全部会话 HTML、Markdown、JSON、CSV、TXT，另可生成
联系人 CSV、全量消息 CSV 和账号隔离的 SQLite 副本。HTML/Markdown ZIP
会包含媒体；SQLite 副本也会复制媒体并重写为包内相对路径。任务完成后可直接
下载，解压后无需启动服务或联网。

如果档案库还不存在，同一条命令会直接打开首次运行向导。向导能检测数据目录
和账号；Linux 读取微信进程内存时会给出一条严格限定参数的 `pkexec` 命令，
只有取钥步骤需要系统授权。后续安全快照、WAL 合并、解密、媒体还原和建索引
都以当前桌面用户运行。

## 本机 M0 验证结果

- 识别 2 个账号，数据库数量分别为 20 和 31。
- 主账号密钥校验 20/20，数据库解密 20/20，失败 0。
- 读取会话 65 个，导出最近有消息会话 1 个。
- 9 条真实消息中：1 张图片解密为 696×1010 JPEG，2 条语音转为 2.232s/4.512s MP3，1 个视频为 720×1280 H.264/AAC MP4，4 个表情实例还原为 48 帧 WebP。
- 另一个真实公众号会话中 3 个 URL 卡片均生成安全可点击链接。
- 富媒体验收器结果为零错误。
- 统一档案首次同步 28 条消息、3,396 个联系人、9 个附件；第二次同步新增 0 并跳过全表扫描。
- 自动化测试 24 项全部通过（含 10 万消息分页、搜索和统计回归）。
- 本地 HTTP 验证通过：无令牌访问返回 401，图片返回正确 MIME，MP3/MP4
  支持浏览器 Range 分段请求并返回 206 与正确 `Content-Range`。
- 统一档案真实导出 HTML/Markdown 各 9 条消息、9 个附件；两种格式均验收为
  1 张可点击图片、2 个可播放音频、1 个可播放视频、1 个动态表情，错误 0。
- 真实全量导出：3,396 个联系人、28 条消息、7 个会话；SQLite 副本仅有
  1 个所选账号，28 条消息、9 个附件、28 条 FTS 记录，9/9 媒体路径有效。
