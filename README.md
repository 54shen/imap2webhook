# imap2webhook

一个轻量级 Docker 服务,用于监听 IMAP 邮箱并将新邮件实时转发到 webhook。之所以构建它,是因为 n8n 自带的 IMAP 节点用起来不如预期。该服务负责「监听 + 结构化转发」部分:把新邮件解析成统一的 JSON 格式 POST 到任意 webhook,另一端(如 n8n、自定义脚本、消息机器人)想怎么处理都行。

*后续计划:nocodenode 上会增加一个轻量级 FastAPI 用来直接操作 IMAP。*

---

## 功能特性

- 📡 **实时监听**:基于 IMAP `IDLE` 命令长连接,收到新邮件推送立即拉取处理,无需轮询
- 📦 **结构化 JSON 负载**:自动解析主题、发件人、收件人、时间、纯文本/HTML 正文、附件(附件以 base64 编码);中文主题和正文按声明字符集正确解码
- 💾 **去重机制**:已处理邮件的 UID 记录在本地 SQLite 数据库中,重启、断线重连都不会重复转发;检测到邮箱重建(UIDVALIDITY 变化)时自动清空记录
- 🔁 **自动重连**:连接异常时退避重试(10 秒起、封顶 60 秒),断线期间到达的邮件会在重连后补发
- 🚀 **webhook 失败重试**:发送失败(网络错误或非 2xx)自动重试,仍失败则保留邮件待下次触发时补发,不丢邮件
- 🔧 **纯配置驱动**:所有行为通过环境变量(或本地 `.env` 文件)控制,无需改代码

## 工作流程

```
┌──────────────┐     ┌───────────────────────┐     ┌──────────────┐
│  IMAP 服务器  │◄───►│   imap2webhook 容器    │────►│  你的 webhook │
│  (邮件)       │     │                       │     │  (n8n 等)     │
└──────────────┘     └───────────────────────┘     └──────────────┘
                        │
                        ▼
                  SQLite (data.db)
                  记录已处理的邮件 UID
```

1. 启动时连接 IMAP 服务器并登录,选中要监听的邮箱(`MAILBOX`)
2. 进入 `IDLE` 长连接状态,等待服务器推送新邮件通知
3. 检测到新邮件(`EXISTS` 通知)后,搜索所有未读邮件 UID
4. 对每个「不在数据库中的」UID:拉取完整邮件 → 解析成 JSON → POST 到 webhook → 将 UID 写入数据库
5. 若连接异常,退避等待后重新连接,断线期间到达的邮件会在重连后补发

### 首次启动行为

启动后的第一次连接,未读邮件的处理方式由 `PAST_UNSEEN` 决定:

| 场景 | 行为 |
|------|------|
| `PAST_UNSEEN=false`(默认) | 仅把邮箱中已有的未读邮件 UID 登记进数据库,**不转发**,只关注之后到达的新邮件 |
| `PAST_UNSEEN=true` | 把邮箱中已有的未读邮件**全部转发**到 webhook(数据库已记录的除外) |
| 断线后重连 | 重连时发现的未读邮件会被转发(视为断线期间到达的邮件) |

## 目录结构

```
imap2webhook/
├── app/                        # 应用主代码
│   ├── main.py                 # 入口:初始化日志 → 创建 EmailManager → 启动主循环
│   ├── manager.py              # 核心管理器:主循环、去重、webhook 投递与重试
│   ├── sqlitedb.py             # SQLite 封装:邮件 UID 与元数据(UIDVALIDITY)存储
│   ├── config/                 # 配置模块
│   │   ├── settings.py         # 从环境变量 / .env 读取配置,并校验必填项
│   │   └── logger.py           # 日志初始化(级别由 LOG_LEVEL 控制)
│   └── imap/                   # IMAP 模块
│       ├── client.py           # ImapClient:连接、选邮箱、搜索/拉取邮件、解析、IDLE 监听
│       └── schemas.py          # Pydantic 数据模型:Attachment、MessageEnvelope(webhook 负载)
├── sender/                     # 推送脚本(每封邮件独立进程运行,改完即时生效)
│   ├── custom_sender.py        # 自定义推送脚本(含密钥,不提交;模板见 custom_sender.py.example)
│   ├── custom_sender.py.example  # 推送脚本模板(可复制的起点)
│   ├── browser_image.py        # 无头浏览器(Edge/Chromium)按浏览器视角把邮件 HTML 渲染成图片
│   ├── table_image.py          # Pillow 兜底渲染(浏览器不可用时的降级方案)
│   └── resend.py               # 手动补发工具(list / info / <uid> / 交互模式)
├── tests/
│   └── test_payload.json       # 推送脚本独立测试用的样例邮件负载(example.com 假数据)
├── Dockerfile                  # python:3.12-slim 镜像,非 root 运行,声明 /app/data 卷
├── .dockerignore               # 排除密钥与本地数据进镜像
├── requirements.txt            # 依赖:requests、pydantic、python-dotenv、Pillow、playwright
├── .env.example                # 配置文件模板(复制为 .env 后填写)
├── start.bat                   # Windows 一键启动脚本
├── README.md                   # 本文档
└── .gitignore
```

### 各模块职责

**app/main.py — 入口**

只有 3 行逻辑:初始化日志 → 实例化 `EmailManager` → 调用 `run()` 进入无限循环。容器中通过 `python -u app/main.py` 启动。

**app/manager.py — 核心管理器 `EmailManager`**

整个服务的心脏,负责编排:

- `run()`:主循环。处理 `FLUSH_DB` 清库 → 连接 IMAP → 首次连接分流(见上文「首次启动行为」)→ 进入 `IDLE` 监听;任何异常都会被捕获,日志报错后退避重连(10s 起、封顶 60s)
- `_check_uidvalidity()`:对比数据库中的 UIDVALIDITY,发现邮箱被重建(UID 会复用)时自动清空 UID 记录
- `manage_unseens()`:遍历未读 UID,**跳过已在数据库中的**,对新的 UID 依次执行:解析 → 转发 → 登记 UID。单封邮件解析失败不会影响其他邮件,失败项下次触发时自动重试
- `send_to_webhook()`:向 `WEBHOOK` 发 POST 请求(`timeout=10`),非 2xx 视为失败;按 `WEBHOOK_RETRIES` 退避重试(2s/4s/8s),全部失败返回 `False`,该 UID **不入库**,保留待下次触发补发

**app/imap/client.py — IMAP 客户端 `ImapClient`**

对 Python 标准库 `imaplib.IMAP4_SSL` 的薄封装(支持 `with` 语句):

- `connect()` / `disconnect()`:建立 SSL 连接并登录(带连接超时;`IMAP_SSL_VERIFY=false` 可跳过证书校验)
- `select_mailbox()`:选中邮箱,返回邮件数量,并读取 UIDVALIDITY
- `fetch_unseen_uids()`:`UID SEARCH UNSEEN` 搜索未读邮件的 UID 集合
- `parse_email()`:`UID FETCH RFC822` 拉取完整邮件,用 `email` 标准库解析;主题/发件人做 RFC 2047 解码(中文不乱码),正文按声明 charset 解码(GBK/GB2312 等),附件 base64 编码(超过 `MAX_ATTACH_MB` 的跳过)
- `idle()`:手动发送 `IDLE` 命令(无需第三方库,每次使用唯一 tag)。socket 超时 29 分钟以应对服务器 30 分钟 IDLE 限制;收到 `EXISTS` 后发送 `DONE` 返回 `True`;超时/中断时先终止 IDLE 再断开连接,由外层干净地重连

**app/imap/schemas.py — 数据模型**

用 Pydantic 定义 webhook 负载结构。`MessageEnvelope.from_` 字段通过别名 `from` 输出,保证 JSON 里是标准字段名 `"from"`。

**app/sqlitedb.py — SQLite 封装 `SqliteDb`**

- 数据库文件路径由 `DB_PATH` 指定(容器内默认 `/app/data/data.db`,必须挂载卷)
- 表:`email_uids(id, uid UNIQUE)` 记录已处理邮件,`meta(key, value)` 存 UIDVALIDITY
- 启动时把所有已记录的 UID 加载进内存集合,查询去重都在内存中进行
- `flush_uids()`:清空所有记录(由 `FLUSH_DB` 或 UIDVALIDITY 变化触发)

**app/config/settings.py — 配置**

启动时读取环境变量并加载 `.env`(如有),`IMAP_HOST` / `IMAP_USER` / `IMAP_PWD` / `WEBHOOK` 为必填项,缺失时记录错误日志并直接退出(退出码 1),避免带错配置运行。

**app/config/logger.py — 日志**

统一日志格式:`%(levelname)-9s | %(name)s | %(message)s`,级别由 `LOG_LEVEL` 控制,输出到标准输出(容器中通过 `docker logs` 查看)。同时固定以 DEBUG 级别写入 `logs/imap2webhook.log`(滚动保留 3×5MB),用于排查所有活动。

**sender/custom_sender.py — 自定义推送脚本**

当 `.env` 设置了 `CUSTOM_SENDER` 时,每封邮件由服务**以子进程方式**运行一次该脚本,邮件 JSON 通过 stdin 传入。因为每封邮件独立启动,修改它**不需要重启服务**,下一封邮件即生效。推送逻辑(正文策略、微信 API、图片渲染)都在这里。

**sender/browser_image.py / sender/table_image.py — 正文图片渲染**

邮件正文带 HTML 且按策略需要发图片时:优先用无头 Edge/Chromium 按「浏览器观看邮件」的视角把 HTML 渲染成高清图片(dpr=3);失败则回退到 Pillow 直接绘制。渲染失败跳过图片,不影响文字消息。

**sender/resend.py — 手动补发工具**

不经过服务、直接按 UID 重推指定邮件(与自动推送共用 `custom_sender.py` 的发送逻辑,发送顺序一致)。不会改变邮件状态。用法见文件头注释。

## Webhook 负载格式

每封新邮件都会向 webhook 发起一次 POST 请求,请求体为以下 JSON:

```json
{
  "uid": "1809",
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
| `WEBHOOK`        | 是       | —                 | 接收新邮件的 POST 请求的 URL                |
| `CUSTOM_SENDER`  | 否       | —                 | 自定义推送脚本路径(见下文),设置后不再直接 POST 到 WEBHOOK |
| `MAILBOX`        | 否       | `INBOX`           | 要监听的邮箱 / 文件夹                       |
| `PAST_UNSEEN`    | 否       | `false`           | 首次连接时是否转发邮箱中已有的未读邮件       |
| `ATTACH`         | 否       | `true`            | 是否将附件以 base64 编码包含在负载中         |
| `MAX_ATTACH_MB`  | 否       | `10`              | 附件大小上限(MB),超过的跳过                |
| `WEBHOOK_RETRIES`| 否       | `3`               | webhook 失败重试次数(2s/4s/8s 退避)         |
| `FLUSH_DB`       | 否       | `false`           | 为 `true` 时启动时清空数据库中的 UID 记录    |
| `LOG_LEVEL`      | 否       | `INFO`            | 日志级别(DEBUG / INFO / WARNING / ERROR)    |
| `DB_PATH`        | 否       | `/app/data/data.db` | SQLite 数据库文件路径(本地运行请改为 `./data/data.db`) |

## 快速开始

### 方式一:Docker(推荐)

```yaml
services:
  imap2webhook:
    image: 
    restart: unless-stopped
    container_name: imap2webhook
    volumes:
      - imap_data:/app/data
    environment:
      IMAP_HOST: mail.emailhost.com
      IMAP_USER: you@yourdomain.com
      IMAP_PWD: yourpassword
      WEBHOOK: https://your-n8n/webhook/xyz
      MAILBOX: INBOX
      PAST_UNSEEN: false
      ATTACH: true
      LOG_LEVEL: INFO
      FLUSH_DB: false
volumes:
  imap_data:

```

```bash
docker compose up -d
docker logs -f imap2webhook
```

### 方式二:本地直接运行(Windows 一键)

**Windows:双击 `start.bat` 即可。**脚本会自动:创建/复用虚拟环境 → 安装依赖 → 复制 `.env.example` 为 `.env`(仅首次)→ 检查配置未填写时提示 → 启动服务。

手动步骤(任意平台):

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt       # Linux / macOS

# 2. 复制配置文件模板并填写(必填:IMAP_HOST / IMAP_USER / IMAP_PWD / WEBHOOK)
copy .env.example .env                          # Windows
cp .env.example .env                            # Linux / macOS

# 3. 启动(用 -m 方式,否则 import app 会失败)
.venv/Scripts/python -m app.main                # Windows
.venv/bin/python -m app.main                    # Linux / macOS
```

> 提示:本地运行时请把 `DB_PATH` 设为 `./data/data.db`(`.env.example` 已包含),否则会尝试写入容器内的 `/app/data/data.db`。`start.bat` 会自动创建 `data/` 目录。

## 自定义推送脚本(CUSTOM_SENDER)

默认行为是「邮件解析成 JSON 后 POST 到 `WEBHOOK`」。如果推送逻辑需要定制(加鉴权头、按发件人过滤、转发到钉钉/企业微信机器人、推给多个地址、加工字段……),可以不写死为 POST,而是交给**你自己的 Python 脚本**处理:

1. 复制模板:`copy sender\custom_sender.py.example sender\custom_sender.py`
2. 在 `.env` 里设置:`CUSTOM_SENDER=./sender/custom_sender.py`
3. 按需修改 `sender\custom_sender.py`(模板里有完整示例)

**脚本约定:**

- 服务启动脚本,邮件负载(JSON)通过 **stdin** 传入,一次调用处理一封邮件
- **退出码 0** = 投递成功 → UID 记入数据库;**退出码非 0** = 失败 → 按 `WEBHOOK_RETRIES` 退避重试,仍失败则保留邮件,下次触发时补发
- 脚本运行在服务同一个虚拟环境的 Python 里,可 `import requests` 等已装依赖
- 可通过 `os.environ` 读取所有环境变量(包括 `.env` 里自定义的项,如 `WEBHOOK_SECRET`)
- 出错时把原因打印到 stderr,服务日志里能看到

> 注意:设置 `CUSTOM_SENDER` 后 `WEBHOOK` 不再是必填,但 `WEBHOOK` 仍会作为环境变量传给脚本,可直接在脚本里读取使用。

## 数据持久化与去重

- 每封成功转发的邮件,其 UID 都会写入 SQLite(路径由 `DB_PATH` 指定)
- 容器部署时数据库文件必须放在**挂载的卷**中,否则容器重建后数据丢失,已处理过的邮件会被重复转发
- 去重以「UID 是否在数据库中」为准,内存集合加速判断;如需清空历史记录,设置 `FLUSH_DB=true` 重启一次,然后改回 `false`
- 邮箱被重建(UIDVALIDITY 变化)时自动清空记录,避免 UID 复用导致漏邮件

## 注意事项

- **webhook 投递为 at-least-once**:发送失败会自动重试 `WEBHOOK_RETRIES` 次,全部失败时该 UID 不会入库,会在下次新邮件到达或重连时自动补发——极端情况下可能重复投递,建议 webhook 端做幂等处理
- **IDLE 会话每 29 分钟自动刷新**:无事件时会主动终止 IDLE 并重建连接,避免服务器 30 分钟 IDLE 限制导致连接状态错乱
- **IDLE 依赖服务器支持**:主流 IMAP 服务器(Gmail、Outlook、自建 Dovecot 等)都支持 `IDLE`,若服务器拒绝该命令,服务会报错并按退避间隔重连
- **首次连接的行为差异**:默认(`PAST_UNSEEN=false`)时邮箱里已有的旧未读邮件只会被登记、不会转发;如果想一启动就把积压的未读邮件全部推给 webhook,首次启动前设置 `PAST_UNSEEN=true`
- **自签名证书**:自建邮件服务器使用自签名证书时,设置 `IMAP_SSL_VERIFY=false`
- **Docker 镜像以非 root 运行**:若旧版容器以 root 写入过卷数据,升级后注意卷内文件属主需为 UID 1000
