# Ubuntu 安装、升级、备份与回滚

本文以当前数据目录 `/data/wechat/xwechat_files` 为例。所有密钥、解密库、索引和
导出默认放在仓库内的 `mvp-output/`，源微信目录只读访问。

## 1. 首次安装

支持 Python 3.10 及以上。先安装系统组件：

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg policykit-1
```

进入项目目录后运行：

```bash
chmod +x tools/install_ubuntu.sh
./tools/install_ubuntu.sh
```

脚本只创建项目内 `.venv`、安装 `requirements-linux-mvp.txt`，并建立权限为
`0700` 的 `mvp-output/`。它不会安装或修改微信，不会写入
`/data/wechat/xwechat_files`，也不会把整个应用以 root 身份运行。

## 2. 启动

```bash
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  web \
  --archive-db mvp-output/archive/archive.sqlite3 \
  --setup-root mvp-output \
  --port 8765
```

复制终端打印的完整本地地址到浏览器。服务固定监听 `127.0.0.1`；关闭终端进程
即可停止。首次没有档案时会进入向导。向导给出的 `pkexec` 命令只为取钥临时
读取微信进程，后续快照、解密、索引和导出均由当前用户完成。

## 3. 日常增量更新

微信产生新消息后，在设置页重新执行初始化/同步流程，或使用：

```bash
.venv/bin/python -m wx4 \
  --data-root /data/wechat/xwechat_files \
  --wxid <账号> \
  archive \
  --decrypted-dir mvp-output/<账号>/decrypted \
  --archive-db mvp-output/archive/archive.sqlite3 \
  --archive-media-dir mvp-output/archive/media \
  --media
```

密钥未变化时无需重复取钥。解密器会为每个库复制包含 `-wal/-shm` 的临时快照，
不会原地修改微信数据库。档案同步通过源文件指纹和稳定消息键去重。

## 4. 升级前备份

先停止本地服务与正在执行的导出任务，然后：

```bash
mkdir -p mvp-backups/before-upgrade
chmod 700 mvp-backups mvp-backups/before-upgrade
sqlite3 mvp-output/archive/archive.sqlite3 \
  ".backup 'mvp-backups/before-upgrade/archive.sqlite3'"
cp -a mvp-output/archive/media mvp-backups/before-upgrade/
cp -a mvp-output/*/keys.json mvp-backups/before-upgrade/ 2>/dev/null || true
chmod -R go-rwx mvp-backups/before-upgrade
sqlite3 mvp-backups/before-upgrade/archive.sqlite3 "pragma integrity_check;"
```

最后一条命令必须输出 `ok`。备份中含聊天正文、媒体和密钥，不能上传网盘或提交
到 Git；如确需异地保存，应先使用可信的全盘或文件级加密。

## 5. 升级

记录当前提交，获取代码，然后重新执行幂等安装脚本：

```bash
git rev-parse HEAD
git pull --ff-only
./tools/install_ubuntu.sh
.venv/bin/python -m unittest discover -s tests -v
```

确认测试通过后再启动服务。统一档案采用版本化 schema；升级前备份是跨版本回滚
的唯一可信边界。

## 6. 回滚

如果新版本不能使用，先停止服务。保留故障现场，不删除任何目录：

```bash
mv mvp-output/archive mvp-output/archive.failed
mkdir -p mvp-output/archive
chmod 700 mvp-output/archive
cp -a mvp-backups/before-upgrade/archive.sqlite3 mvp-output/archive/
cp -a mvp-backups/before-upgrade/media mvp-output/archive/
chmod -R go-rwx mvp-output/archive
git switch --detach <升级前记录的提交>
./tools/install_ubuntu.sh
```

验证旧版本正常后，再人工比较 `archive.failed`；不要直接覆盖或删除它。

## 7. 健康检查

```bash
sqlite3 mvp-output/archive/archive.sqlite3 "pragma integrity_check;"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/audit_privacy.py \
  --private-root mvp-output \
  --export mvp-output/archive/exports/<导出任务>.zip
```

数据库检查应输出 `ok`，测试应全部通过，隐私审计的 `errors` 应为空数组。

## 8. 常见故障

- 找不到账号：确认路径正好是 `xwechat_files` 或它的父目录，且当前用户有读权限。
- 取钥被拒绝：确认微信已登录；只运行向导生成的受限 `pkexec` 命令，不要用
  `sudo` 启动整个 Web 服务。
- 图片/语音缺失：先在微信中打开对应资源让客户端完成下载，再做增量同步。
- 端口占用：改用 `--port 8766`；仍只监听本机回环地址。
- 页面提示令牌无效：关闭旧标签页，重新使用本次启动打印的完整地址。
