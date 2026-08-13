#!/usr/bin/env python
"""
imap2webhook 邮件补发工具
========================================
按 UID 把指定邮件重新推送到微信(与自动推送共用 custom_sender.py 的逻辑)。

用法(在项目根目录下运行):
    python resend.py list                    # 列默认账户最近的邮件
    python resend.py list 1                  # 列账户 1 最近的邮件
    python resend.py 879                     # 推送默认账户 UID=879 的邮件
    python resend.py 879 1                   # 推送账户 1 UID=879 的邮件
    python resend.py info 879                # 查看 MIME 结构和解析结果(默认账户)
    python resend.py info 879 1              # 同上,账户 1
    python resend.py                         # 不带参数进入交互模式(可连续发送)

说明:
- 账户按 .env 的 IMAP1_* / IMAP2_* 编号(编号必须连续),账户名 = 邮箱地址;
  参数可用编号(1/2/3…)或邮箱地址
- 读取 .env 里的 IMAP 配置和 custom_sender.py 的推送配置
- 不改变邮件状态(不会标记已读、不会移动邮件)
- 与自动推送的差异:不管数据库记录,指定哪封就发哪封
"""
import base64
import email
import os
import sys
import time

# 核心实现(app 包)在 src/ 下 —— 注入到 sys.path 后 from app.xxx 原样可用
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from app.config.settings import AccountConfig, settings
from app.imap.client import ImapClient

import custom_sender        # 与根目录的推送脚本共用逻辑(其内部会注入 src/sender 渲染模块)


def _resolve_account(account=None) -> AccountConfig:
    """账户参数 → AccountConfig。支持邮箱地址(精确匹配)或编号(1/2/3…);空 = 第 1 个。"""
    if isinstance(account, AccountConfig):
        return account
    accounts = settings.load_accounts()
    if not accounts:
        raise SystemExit("没有配置任何账户:请在 .env 中配置 IMAP1_* / IMAP2_*(编号必须连续)。")
    if account in (None, ""):
        return accounts[0]
    if str(account).isdigit() and 1 <= int(account) <= len(accounts):
        return accounts[int(account) - 1]
    for a in accounts:
        if a.name == str(account):
            return a
    listing = ", ".join(f"{i + 1} {a.name}" for i, a in enumerate(accounts))
    raise SystemExit(f"账户 {account!r} 不存在。可用账户: {listing}")


def list_recent(count: int = 20, account=None) -> None:
    """列出邮箱最近的邮件,方便挑选要补发的 UID"""
    account = _resolve_account(account)
    with ImapClient(account) as client:
        client.select_mailbox(account.mailbox)
        _, data = client._conn.uid("search", None, "ALL")
        uids = data[0].split()[-count:]
        print(f"邮箱 {account.mailbox}(账户 {account.name}) 最近 {len(uids)} 封邮件:")
        for uid in uids:
            _, fetch = client._conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)])")
            msg = email.message_from_bytes(fetch[0][1])
            subject = ImapClient._decode_header(msg.get("Subject", ""))[:40]
            print(f"  {uid.decode():>6} | {msg.get('Date', ''):25} | {subject}")


def _dump_mime(msg, indent: int = 0) -> None:
    """打印邮件的 MIME 部件树(排查"没正文/没附件"用)"""
    cd = msg.get_content_disposition()
    fn = msg.get_filename()
    if msg.is_multipart():
        info = "容器"
        print("  " * indent + f"{msg.get_content_type():30} | {info}")
        for part in msg.get_payload():
            _dump_mime(part, indent + 1)
    else:
        raw = msg.get_payload(decode=True)
        size = f"{len(raw)} bytes" if raw is not None else "无内容"
        print("  " * indent + f"{msg.get_content_type():30} | disposition={cd} | filename={fn} | {size}")


def show_info(uid: str, account=None) -> int:
    """查看邮件的 MIME 结构和解析结果(排查为什么没正文/没附件)"""
    if not uid.isdigit():
        print("UID 必须是数字。")
        return 1
    account = _resolve_account(account)
    with ImapClient(account) as client:
        client.select_mailbox(account.mailbox)
        status, data = client._conn.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not data or data[0] is None:
            print(f"拉取失败:UID {uid} 不存在。")
            return 1
        msg = email.message_from_bytes(data[0][1])
        print(f"=== 邮件 {uid}(账户 {account.name})的 MIME 结构 ===")
        _dump_mime(msg)
        print(f"=== 解析结果 ===")
        payload = client.parse_email(uid)
    data = payload.model_dump(by_alias=True)
    print(f"账户: {data.get('account')}")
    print(f"主题: {data.get('subject')}")
    print(f"text_body 长度: {len(data.get('text_body') or '')}")
    print(f"html_body 长度: {len(data.get('html_body') or '')}")
    print(f"附件数量: {len(data.get('attachments', []))}")
    for att in data.get("attachments", []):
        print(f"  - {att['filename']} ({att['content_type']})")
    return 0


def send_by_uid(uid: str, account=None) -> int:
    if not uid.isdigit():
        print("UID 必须是数字。")
        return 1
    account = _resolve_account(account)
    print(f"正在从 {account.imap_host}(账户 {account.name})拉取 UID {uid}...", file=sys.stderr)
    with ImapClient(account) as client:
        client.select_mailbox(account.mailbox)
        payload = client.parse_email(uid)              # MessageEnvelope
    data = payload.model_dump(by_alias=True)
    print(f"已获取: {data.get('subject')} | {data.get('from')}", file=sys.stderr)

    ok = True

    # 与自动推送完全一致(含英文翻译 + 正文决策):文字 → 100ms → 图片/附件
    payload, body_text, render_html = custom_sender.prepare_payload(data)
    if not custom_sender.send_text(payload, body_text):
        ok = False
    time.sleep(0.1)
    if not custom_sender.send_body_image(payload, render_html):
        ok = False
    for att in data.get("attachments", []):
        try:
            raw = base64.b64decode(att.get("data", ""))
        except Exception as e:
            print(f"附件 {att.get('filename')} 解码失败,跳过: {e}", file=sys.stderr)
            continue
        if not custom_sender.send_attachment(
            att.get("filename", "未命名"),
            att.get("content_type", "application/octet-stream"),
            raw,
        ):
            ok = False
    print("发送完成" if ok else "部分发送失败", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        list_recent(account=sys.argv[2] if len(sys.argv) >= 3 else None)
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "info":
        uid_arg = sys.argv[2] if len(sys.argv) >= 3 else input("UID> ").strip()
        return show_info(uid_arg, sys.argv[3] if len(sys.argv) >= 4 else None)
    if len(sys.argv) >= 2:
        # 命令行直接指定 UID:发送一次即退出
        return send_by_uid(sys.argv[1], sys.argv[2] if len(sys.argv) >= 3 else None)

    # 交互模式:发完一封可以继续选择下一封
    accounts = settings.load_accounts()
    print("可用账户:")
    for i, a in enumerate(accounts, 1):
        print(f"  {i} {a.name}")
    account = _resolve_account(input(f"账户> ").strip() or None)
    print(f"交互模式:账户 {account.name}。输入 UID 发送邮件,list 查看最近邮件,"
          f"info <UID> 查看邮件详情,quit 退出。")
    while True:
        try:
            line = input("UID> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in ("quit", "q", "exit"):
            return 0
        parts = line.split(maxsplit=1)
        if parts[0].lower() == "list":
            list_recent(account=account)
            continue
        if parts[0].lower() == "info":
            uid_arg = parts[1].strip() if len(parts) > 1 else input("UID> ").strip()
            show_info(uid_arg, account)
            continue
        if not line.isdigit():
            print("UID 必须是数字(或输入 list / info / quit)。")
            continue
        send_by_uid(line, account)
        print()


if __name__ == "__main__":
    sys.exit(main())
