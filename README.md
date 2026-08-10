# imap2webhook

一个轻量级服务,用于监听 IMAP 邮箱并将新邮件实时转发到微信/钉钉等任意目标。之所以构建它,是因为 n8n 自带的 IMAP 节点用起来不如预期。该服务负责「监听 + 结构化转发」部分:把新邮件解析成统一的 JSON 格式交给自定义推送脚本(`custom_sender.py`),脚本想怎么处理都行(推微信、发钉钉、存库……)。

*后续计划:nocodenode 上会增加一个轻量级 FastAPI 用来直接操作 IMAP。*

---

## 功能特性

- 📡 **实时监听**:基于 IMAP `IDLE` 命令长连接,收到新邮件推送立即拉取处理,无需轮询
- 📦 **结构化 JSON 负载**:自动解析主题、发件人、收件人、时间、纯文本/HTML 正文、附件(附件以 base64 编码);中文主题和正文按声明字符集正确解码
- 💾 **去重机制**:已处理邮件的 UID 记录在本地 SQLite 数据库中,重启、断线重连都不会重复转发;检测到邮箱重建(UIDVALIDITY 变化)时自动清空记录
- 🔁 **自动重连**:连接异常时退避重试(10 秒起、封顶 60 秒),断线期间到达的邮件会在重连后补发
- 🚀 **推送失败重试**:发送失败自动重试(2s/4s/8s 退避),仍失败则保留邮件待下次触发时补发,不丢邮件
- 🔧 **纯配置驱动**:所有行为通过环境变量(或本地 `.env` 文件)控制,无需改代码

## 工作流程

```
┌──────────────┐     ┌───────────────────────┐     ┌────────────────────────┐
│  IMAP 服务器  │◄───►│   imap2webhook 服务    │────►│  推送脚本 custom_sender │
│  (邮件)       │     │                       │     │  (推送到微信/钉钉等)    │
└──────────────┘     └───────────────────────┘     └────────────────────────┘
                        │
                        ▼
                  SQLite (data.db)
                  记录已处理的邮件 UID
```

1. 启动时连接 IMAP 服务器并登录,选中要监听的邮箱(`MAILBOX`)
2. 进入 `IDLE` 长连接状态,等待服务器推送新邮件通知
3. 检测到新邮件(`EXISTS` 通知)后,搜索所有未读邮件 UID
4. 对每个「不在数据库中的」UID:拉取完整邮件 → 解析成 JSON → 交给推送脚本 → 将 UID 写入数据库
5. 若连接异常,退避等待后重新连接,断线期间到达的邮件会在重连后补发

### 首次启动行为

启动后的第一次连接,未读邮件的处理方式由 `PAST_UNSEEN` 决定:

| 场景 | 行为 |
|------|------|
| `PAST_UNSEEN=false`(默认) | 仅把邮箱中已有的未读邮件 UID 登记进数据库,**不转发**,只关注之后到达的新邮件 |
| `PAST_UNSEEN=true` | 把邮箱中已有的未读邮件**全部推送**(数据库已记录的除外) |
| 断线后重连 | 重连时发现的未读邮件会被转发(视为断线期间到达的邮件) |

## 目录结构

```
imap2webhook/
├── main.py                     # 入口薄壳:注入 src/ 到 sys.path → 复用 app 包启动主循环
├── custom_sender.py            # 自定义推送脚本(最常改,改完即时生效;含密钥,不提交;
│                               #   模板见 src/sender/custom_sender.py.example)
├── resend.py                   # 手动补发工具(list / info / <uid> / 交互模式)
├── start.bat                   # Windows 一键启动脚本
├── .env / .env.example         # 本地配置(模板)/ 配置模板
├── README.md                   # 本文档
├── requirements.txt            # 依赖:requests、pydantic、python-dotenv、Pillow、playwright
├── .gitignore
└── src/                        # 实现代码(根目录三个入口脚本各自注入 src/ 到 sys.path)
    ├── app/                    # 应用主代码(app 包名)
    │   ├── manager.py          # 核心管理器:主循环、去重、推送与重试(每账户一线程)
    │   ├── sqlitedb.py         # SQLite 封装:邮件 UID 与元数据(UIDVALIDITY)存储
    │   ├── config/             # 配置模块
    │   │   ├── settings.py     # 从环境变量 / .env 读取配置,并校验必填项
    │   │   └── logger.py       # 日志初始化(级别由 LOG_LEVEL 控制)
    │   └── imap/               # IMAP 模块
    │       ├── client.py       # ImapClient:连接、选邮箱、搜索/拉取邮件、解析、IDLE 监听
    │       └── schemas.py      # Pydantic 数据模型:Attachment、MessageEnvelope(推送负载)
    ├── sender/                 # 推送脚本的渲染辅助(每封邮件独立进程运行,改完即时生效)
    │   ├── custom_sender.py.example  # 推送脚本模板(可复制的起点)
    │   ├── browser_image.py    # 无头浏览器(Edge/Chromium)按浏览器视角把邮件 HTML 渲染成图片
    │   └── table_image.py      # Pillow 兜底渲染(浏览器不可用时的降级方案)
    └── tests/
        └── test_payload.json   # 推送脚本独立测试用的样例邮件负载(example.com 假数据)
```

### 各模块职责

**main.py — 入口**

根目录薄壳:把 `src/` 注入 `sys.path`(保留 `app` 包名)→ 初始化日志 → 实例化 `EmailManager` → 调用 `run()` 进入无限循环。用 `python main.py` 启动(必须在项目根目录)。

**src/app/manager.py — 核心管理器 `EmailManager`**

整个服务的心脏,负责编排:

- `run()`:主循环。处理 `FLUSH_DB` 清库 → 连接 IMAP → 首次连接分流(见上文「首次启动行为」)→ 进入 `IDLE` 监听;任何异常都会被捕获,日志报错后退避重连(10s 起、封顶 60s)
- `_check_uidvalidity()`:对比数据库中的 UIDVALIDITY,发现邮箱被重建(UID 会复用)时自动清空 UID 记录
- `manage_unseens()`:遍历未读 UID,**跳过已在数据库中的**,对新的 UID 依次执行:解析 → 转发 → 登记 UID。单封邮件解析失败不会影响其他邮件,失败项下次触发时自动重试
- `send_payload()`:以子进程运行自定义推送脚本(邮件 JSON 从 stdin 传入,**退出码 0** = 投递成功);按 `PUSH_RETRIES` 退避重试(2s/4s/8s),全部失败返回 `False`,该 UID **不入库**,保留待下次触发补发

**src/app/imap/client.py — IMAP 客户端 `ImapClient`**

对 Python 标准库 `imaplib.IMAP4_SSL` 的薄封装(支持 `with` 语句):

- `connect()` / `disconnect()`:建立 SSL 连接并登录(带连接超时;`IMAP_SSL_VERIFY=false` 可跳过证书校验)
- `select_mailbox()`:选中邮箱,返回邮件数量,并读取 UIDVALIDITY
- `fetch_unseen_uids()`:`UID SEARCH UNSEEN` 搜索未读邮件的 UID 集合
- `parse_email()`:`UID FETCH RFC822` 拉取完整邮件,用 `email` 标准库解析;主题/发件人做 RFC 2047 解码(中文不乱码),正文按声明 charset 解码(GBK/GB2312 等),附件 base64 编码(超过 `MAX_ATTACH_MB` 的跳过)
- `idle()`:手动发送 `IDLE` 命令(无需第三方库,每次使用唯一 tag)。socket 超时 29 分钟以应对服务器 30 分钟 IDLE 限制;收到 `EXISTS` 后发送 `DONE` 返回 `True`;超时/中断时先终止 IDLE 再断开连接,由外层干净地重连

**src/app/imap/schemas.py — 数据模型**

用 Pydantic 定义推送负载结构。`MessageEnvelope.from_` 字段通过别名 `from` 输出,保证 JSON 里是标准字段名 `"from"`。

**src/app/sqlitedb.py — SQLite 封装 `SqliteDb`**

- 数据库文件路径由 `DB_PATH` 指定(默认 `./data/data.db`,`start.bat` 会自动创建 `data/` 目录)
- 表:`email_uids(id, uid UNIQUE)` 记录已处理邮件,`meta(key, value)` 存 UIDVALIDITY
- 启动时把所有已记录的 UID 加载进内存集合,查询去重都在内存中进行
- `flush_uids()`:清空所有记录(由 `FLUSH_DB` 或 UIDVALIDITY 变化触发)

**src/app/config/settings.py — 配置**

启动时读取环境变量并加载 `.env`(如有),`IMAP_HOST` / `IMAP_USER` / `IMAP_PWD` / `CUSTOM_SENDER` 为必填项,缺失时记录错误日志并直接退出(退出码 1),避免带错配置运行。

**src/app/config/logger.py — 日志**

统一日志格式:`%(levelname)-9s | %(name)s | %(message)s`,级别由 `LOG_LEVEL` 控制,输出到控制台(运行 `start.bat` 的窗口可见)。同时固定以 DEBUG 级别写入 `logs/imap2webhook.log`(滚动保留 3×5MB),用于排查所有活动。

**custom_sender.py — 自定义推送脚本**

当 `.env` 设置了 `CUSTOM_SENDER` 时,每封邮件由服务**以子进程方式**运行一次该脚本(脚本在项目根目录),邮件 JSON 通过 stdin 传入。因为每封邮件独立启动,修改它**不需要重启服务**,下一封邮件即生效。推送逻辑(正文策略、微信 API、图片渲染)都在这里。它内部把 `src/sender/` 注入 `sys.path` 以导入渲染模块。

**src/sender/browser_image.py / src/sender/table_image.py — 正文图片渲染**

邮件正文带 HTML 且按策略需要发图片时:优先用无头 Edge/Chromium 按「浏览器观看邮件」的视角把 HTML 渲染成高清图片(dpr=3);失败则回退到 Pillow 直接绘制。渲染失败跳过图片,不影响文字消息。

**resend.py — 手动补发工具**

不经过服务、直接按 UID 重推指定邮件(与自动推送共用 `custom_sender.py` 的发送逻辑,发送顺序一致)。不会改变邮件状态。用法见文件头注释。

## 推送负载格式

每封新邮件都会被解析成以下 JSON,通过 stdin 交给推送脚本(`CUSTOM_SENDER`):

```json
{
  "uid": "1809",
  "account": "default",
  "subject": "Your invoice is ready",
  "from": "billing@example.com",
  "to": "filter@mydomain.com",
  "cc": "finance@example.com",
  "reply_to": "billing@example.com",
  "date": "2025-01-15T14:32:00",
  "message_id": "<CAB12345@mail.example.com>",
  "in_reply_to": "<OLD67890@mail.example.com>",
  "references": "<OLD67890@mail.example.com>",
  "priority": "1 (Highest)",
  "x_mailer": "Postfix",
  "organization": "Example Corp",
  "return_path": "<bounce@example.com>",
  "sender": "billing@example.com",
  "delivered_to": "filter@mydomain.com",
  "x_original_to": "filter@mydomain.com",
  "authentication_results": "mx.example.com; spf=pass smtp.mailfrom=example.com; dkim=pass header.i=@example.com; dmarc=pass",
  "list_id": "<news.example.com>",
  "list_unsubscribe": "<mailto:unsub@example.com>",
  "content_language": "zh-CN",
  "disposition_notification_to": "billing@example.com",
  "thread_topic": "Re: Your invoice",
  "keywords": "invoice, 2025",
  "text_body": "Please find your invoice...",
  "html_body": "<p>Please find your invoice...</p>",
  "attachments": [
    {
      "filename": "invoice.pdf",
      "content_type": "application/pdf",
      "data": "<base64 encoded content>"
    }
  ],
  "headers": {
    "received": "from mail.example.com (mail.example.com [1.2.3.4]) by mx.example.com (Postfix) with ESMTP id ABC123...",
    "mime-version": "1.0",
    "...": "全部原始邮件头,键名小写,重复头(如 Received、DKIM-Signature)用换行拼接"
  }
}
```

字段说明(邮件头不存在的字段为 `null`):

| 字段 | 说明 |
|------|------|
| `uid` | 邮件在邮箱中的唯一标识(字符串) |
| `account` | 来源账户名(`default` / `1` / `2`...,多账户时区分来源) |
| `subject` | 主题(RFC 2047 已解码,中文正常) |
| `from` / `to` / `cc` | 发件人 / 收件人 / 抄送 |
| `reply_to` / `sender` | 回复地址 / 实际发件人(Sender 头) |
| `date` | 邮件头中的 Date 字段原文(如 `Tue, 14 Jan 2025 14:32:00`) |
| `message_id` | 邮件唯一 ID |
| `in_reply_to` / `references` | 回复的上一封邮件 / 关联邮件链 |
| `priority` | 优先级(X-Priority 或 Importance) |
| `x_mailer` | 发件邮件客户端 |
| `organization` | 发件组织 |
| `return_path` / `delivered_to` / `x_original_to` | 退信地址 / 送达地址 / 原收件人 |
| `authentication_results` | 认证结果(SPF / DKIM / DMARC) |
| `list_id` / `list_unsubscribe` | 邮件列表标识 / 退订链接 |
| `content_language` | 邮件语言 |
| `disposition_notification_to` | 已读回执地址 |
| `thread_topic` / `keywords` | 线程主题 / 关键词 |
| `text_body` / `html_body` | 纯文本正文 / HTML 正文(多段部件会拼接) |
| `attachments` | 附件数组;`ATTACH=false` 时为空数组,`filename` 缺失时记 `"unnamed"` |
| `headers` | **全部原始邮件头**字典(键名小写,重复头用换行拼接,内容未解码) |

## 环境变量

| 变量             | 是否必填 | 默认值            | 说明                                        |
|------------------|----------|-------------------|---------------------------------------------|
| `IMAP_HOST`      | 是       | —                 | IMAP 服务器主机名                           |
| `IMAP_PORT`      | 否       | `993`             | IMAP 服务器端口(SSL)                       |
| `IMAP_USER`      | 是       | —                 | 邮箱地址 / 登录名                           |
| `IMAP_PWD`       | 是       | —                 | 账户密码                                    |
| `IMAP_SSL_VERIFY`| 否       | `true`            | 设为 `false` 跳过证书校验(自签名证书)      |
| `IMAP_TIMEOUT`   | 否       | `30`              | IMAP 连接超时(秒)                          |
| `CUSTOM_SENDER`  | 是       | —                 | 自定义推送脚本路径(唯一推送通道,见下文) |
| `MAILBOX`        | 否       | `INBOX`           | 要监听的邮箱 / 文件夹                       |
| `PAST_UNSEEN`    | 否       | `false`           | 首次连接时是否转发邮箱中已有的未读邮件       |
| `ATTACH`         | 否       | `true`            | 是否将附件以 base64 编码包含在负载中         |
| `MAX_ATTACH_MB`  | 否       | `10`              | 附件大小上限(MB),超过的跳过                |
| `PUSH_RETRIES`   | 否       | `3`               | 推送失败重试次数(2s/4s/8s 退避)            |
| `FLUSH_DB`       | 否       | `false`           | 为 `true` 时启动时清空数据库中的 UID 记录    |
| `LOG_LEVEL`      | 否       | `INFO`            | 日志级别(DEBUG / INFO / WARNING / ERROR)    |
| `DB_PATH`        | 否       | `./data/data.db` | SQLite 数据库文件路径 |

## 多账户(同时监听多个邮箱)

在 `.env` 中把账户配置复制一组、编号 +1,即可同时监听多个邮箱:

```ini
# 账户 0(无前缀,默认)— 始终存在,不能删
IMAP_HOST=imap.exmail.qq.com
IMAP_USER=xxx@54shen.cn
IMAP_PWD=xxx

# 账户 1 — IMAP1_* 前缀
IMAP1_HOST=imap.qq.com
IMAP1_USER=xxx@qq.com
IMAP1_PWD=xxx
IMAP1_MAILBOX=INBOX        # 可选,未配置继承全局默认
IMAP1_PAST_UNSEEN=false    # 可选,未配置继承全局默认

# 账户 2 — IMAP2_* 前缀,以此类推
```

- **编号必须连续**:有 `IMAP1_*` 没有 `IMAP2_*` 时,`IMAP3_*` 及之后的账户不会被加载
- 未配置的项继承全局默认值(`IMAP_PORT`/`IMAP_TIMEOUT`/`MAILBOX`/`PAST_UNSEEN`/`ATTACH`/`MAX_ATTACH_MB`)
- 每个账户**独立线程**监听,互不影响:一个账户断线/登录失败只重试它自己
- 去重按账户隔离(同一 UID 在不同账户互不干扰),历史记录自动迁移到默认账户
- 每封邮件的 JSON 负载带 `account` 字段(账户名 `default` / `1` / `2`...),推送脚本可据此区分来源

## 快速开始

### 本地运行(Windows 一键)

**Windows:双击 `start.bat` 即可。**脚本会自动:创建/复用虚拟环境 → 安装依赖 → 复制 `.env.example` 为 `.env`(仅首次)→ 检查配置未填写时提示 → 启动服务。

手动步骤(任意平台):

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt       # Linux / macOS

# 2. 复制配置文件模板并填写(必填:IMAP_HOST / IMAP_USER / IMAP_PWD / CUSTOM_SENDER)
copy .env.example .env                          # Windows
cp .env.example .env                            # Linux / macOS

# 3. 启动(main.py 在根目录,内部注入 src/ 后复用 app 包)
.venv/Scripts/python main.py                    # Windows
.venv/bin/python main.py                        # Linux / macOS
```

> 提示:`.env.example` 中 `DB_PATH` 已设为 `./data/data.db`,`start.bat` 会自动创建 `data/` 目录。

## Linux 部署(systemd 开机自启)

以 `/root/imap2webhook` 为例(路径可换)。

### 1. 克隆 + 安装依赖

```bash
git clone https://github.com/54shen/imap2webhook.git
cd ~/imap2webhook
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt          # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 配置(必填)

> ⚠️ 两个 `cp` 缺一不可,漏掉任何一个服务都起不来(报错见第 5 节对照表);
> `custom_sender.py` 必须放在**项目根目录**(不是 `src/` 下)。

```bash
cp .env.example .env
cp src/sender/custom_sender.py.example custom_sender.py   # 推送脚本含密钥,不入仓库
mkdir -p data                                            # 数据库目录(logs/ 启动时自动创建)
```

- 编辑 `.env`:必填 `IMAP_HOST` / `IMAP_USER` / `IMAP_PWD` / `CUSTOM_SENDER`(`CUSTOM_SENDER` 保持 `./custom_sender.py`,相对 `WorkingDirectory` 解析)
- 编辑 `custom_sender.py`:填 API 密钥(文件头「配置区」),改完**下一封邮件即生效,无需重启服务**

### 3. 创建 systemd 服务

⚠ `ExecStart` 必须用 venv 里的 python(不是系统 python3,否则依赖找不到):

```bash
cat > /etc/systemd/system/imap2webhook.service << 'EOF'
[Unit]
Description=imap2webhook - IMAP to WeChat forwarding
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/imap2webhook
ExecStart=/root/imap2webhook/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 4. 启动 + 开机自启

```bash
systemctl daemon-reload
systemctl enable imap2webhook
systemctl start imap2webhook
systemctl status imap2webhook        # 确认 active (running)
```

### 5. 验证

本服务**只主动连接 IMAP 服务器,不监听任何入站端口**,无需放行防火墙。

```bash
journalctl -u imap2webhook -f        # 实时日志
```

正常启动日志:账户启动 → `Unseen emails at startup: N`(登记已有未读)→ 进入 IDLE 监听;新邮件到达会看到解析与推送记录。**收不到微信提醒时先看这里**。

**常见报错对照表**(`journalctl -u imap2webhook -n 30` 查看):

| journalctl 报错 | 原因 | 解决 |
|---|---|---|
| `Missing mandatory environment variables: IMAP_HOST, IMAP_USER, IMAP_PWD, CUSTOM_SENDER` | `.env` 未复制或未填写 | `cp .env.example .env` 后填四个必填项,重启 |
| `CUSTOM_SENDER file not found: ./custom_sender.py` | 根目录缺推送脚本,或放到了 `src/` 下 | `cp src/sender/custom_sender.py.example custom_sender.py` 并填密钥,必须放项目根目录 |
| `unable to open database file` | `data/` 目录不存在 | `mkdir -p data`,重启 |

### 6. 更新与运维

```bash
# 一键更新:拉代码 + 装依赖 + 重启
cd ~/imap2webhook && git pull && venv/bin/pip install -r requirements.txt && systemctl restart imap2webhook

# 看日志
journalctl -u imap2webhook -f
journalctl -u imap2webhook -n 100    # 最近 100 行
```

Linux 部署注意事项:

- **HTML 正文图片渲染**:优先用无头浏览器(需 `venv/bin/playwright install chromium`);未安装时自动回退 Pillow 渲染,但需中文字体(如 `apt install fonts-noto-cjk`)
- **数据都在项目目录**:`data/`(去重数据库)与 `logs/`(调试日志),备份 = 拷贝这两个目录
- **断线自动恢复**:IMAP 断线按退避自动重连,IDLE 每 10 分钟主动刷新,无需额外守护

## 自定义推送脚本(CUSTOM_SENDER)

推送由**你自己的 Python 脚本**完成——每封邮件都会被服务以子进程方式运行一次该脚本,邮件 JSON 从 stdin 传入。推送逻辑(加鉴权头、按发件人过滤、转发到微信/钉钉/企业微信机器人、推给多个地址、加工字段……)全部由脚本决定,改完立即生效,无需重启服务:

1. 复制模板:`copy src\sender\custom_sender.py.example custom_sender.py`
2. 在 `.env` 里设置:`CUSTOM_SENDER=./custom_sender.py`
3. 按需修改根目录的 `custom_sender.py`(模板里有完整示例)

**脚本约定:**

- 服务启动脚本,邮件负载(JSON)通过 **stdin** 传入,一次调用处理一封邮件
- **退出码 0** = 投递成功 → UID 记入数据库;**退出码非 0** = 失败 → 按 `PUSH_RETRIES` 退避重试,仍失败则保留邮件,下次触发时补发
- 脚本运行在服务同一个虚拟环境的 Python 里,可 `import requests` 等已装依赖
- 可通过 `os.environ` 读取所有环境变量(包括 `.env` 里自定义的项,如 `PUSH_URL`)
- 出错时把原因打印到 stderr,服务日志里能看到

## 数据持久化与去重

- 每封成功转发的邮件,其 UID 都会写入 SQLite(路径由 `DB_PATH` 指定)
- 去重以「(账户, UID) 是否在数据库中」为准,内存集合加速判断;多账户各自独立去重;如需清空历史记录,设置 `FLUSH_DB=true` 重启一次,然后改回 `false`
- 邮箱被重建(UIDVALIDITY 变化)时自动清空该账户的记录,避免 UID 复用导致漏邮件

## 注意事项

- **推送为 at-least-once**:发送失败会自动重试 `PUSH_RETRIES` 次,全部失败时该 UID 不会入库,会在下次新邮件到达或重连时自动补发——极端情况下可能重复投递,建议接收端做幂等处理
- **IDLE 每 10 分钟主动刷新**:腾讯企业邮箱实测约 12-13 分钟会到期关闭 IDLE,服务提前到 10 分钟主动结束 IDLE(干净断开)并**立即重连**(不等退避),避免连接状态错乱;断档期到达的邮件由重连后的未读扫描兜底
- **IDLE 依赖服务器支持**:主流 IMAP 服务器(Gmail、Outlook、自建 Dovecot 等)都支持 `IDLE`,若服务器拒绝该命令,服务会报错并按退避间隔重连
- **首次连接的行为差异**:默认(`PAST_UNSEEN=false`)时邮箱里已有的旧未读邮件只会被登记、不会转发;如果想一启动就把积压的未读邮件全部推送,首次启动前设置 `PAST_UNSEEN=true`
- **自签名证书**:自建邮件服务器使用自签名证书时,设置 `IMAP_SSL_VERIFY=false`
