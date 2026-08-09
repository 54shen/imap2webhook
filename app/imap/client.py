import base64
import email.header
import imaplib
import logging
import time
import email
from app.config.settings import settings
from app.imap.schemas import MessageEnvelope, Attachment

logger = logging.getLogger(__name__)

class ImapClient:
    """
    Thin wrapper around imaplib.IMAP4_SSL.
    Handles connection, login, and reconnection.
    Use as a context manager : with ImapClient() as client:
    """

    # Servers typically end IDLE sessions after ~30 minutes of inactivity;
    # wake up before that and let the caller re-establish a fresh IDLE.
    IDLE_TIMEOUT_SECONDS = 29 * 60

    def __init__(self):
        self._conn: imaplib.IMAP4_SSL | None = None
        self.uidvalidity: int | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        logger.debug("Opening IMAP connection to %s:%s", settings.IMAP_HOST, settings.IMAP_PORT)
        # A connect timeout keeps the service from hanging forever when the server is unreachable.
        self._conn = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=settings.IMAP_TIMEOUT)
        self._conn.login(settings.IMAP_USER, settings.IMAP_PWD)
        logger.debug("IMAP login successful for %s", settings.IMAP_USER)

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
                logger.debug("IMAP connection closed cleanly")
            except Exception:
                logger.debug("IMAP connection closed with error (ignored)")
            finally:
                self._conn = None

    # ------------------------------------------------------------------
    # Mailbox helpers
    # ------------------------------------------------------------------

    def select_mailbox(self, mailbox: str = "INBOX") -> int:
        """Selects a mailbox and returns the message count."""
        status, data = self._conn.select(mailbox)
        if status != "OK":
            raise ValueError(f"Cannot select mailbox '{mailbox}': {data}")
        count = int(data[0])
        self.uidvalidity = self._extract_uidvalidity(mailbox, data)
        logger.debug("Selected mailbox '%s' (%d messages)", mailbox, count)
        return count

    def _extract_uidvalidity(self, mailbox: str, data) -> int | None:
        """UIDVALIDITY is usually part of the untagged SELECT response; fall back to STATUS."""
        for line in data[1:]:
            try:
                parts = line.decode().split()
                if parts and parts[0].upper() == "UIDVALIDITY":
                    return int(parts[1])
            except (ValueError, IndexError, UnicodeDecodeError):
                continue
        try:
            _, status_data = self._conn.status(mailbox, "(UIDVALIDITY)")
            raw = status_data[0].decode()
            return int(raw.split("UIDVALIDITY", 1)[1].strip().strip(")").strip())
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    def fetch_unseen_uids(self):
        status, data = self._conn.uid("search", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            logger.debug("UID SEARCH UNSEEN: 无结果")
            return set()
        uids = set(data[0].split())
        logger.debug("UID SEARCH UNSEEN: %d 封未读 -> %s", len(uids),
                     b",".join(sorted(uids)).decode()[:200])
        return uids


    def parse_email(self, uid: str) -> MessageEnvelope:
        logger.debug("拉取邮件 UID %s (RFC822)", uid)
        status, data = self._conn.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not data or data[0] is None:
            raise ValueError(f"UID {uid} not found in '{settings.MAILBOX}'")
        logger.debug("邮件 UID %s 拉取完成(%d 字节),开始解析", uid, len(data[0][1]))

        msg = email.message_from_bytes(data[0][1])
        payload = MessageEnvelope(uid=uid)
        payload.subject = self._decode_header(msg.get("Subject", ""))
        payload.from_ = self._decode_header(msg.get("From", ""))
        payload.to = self._decode_header(msg.get("To", ""))
        payload.cc = self._decode_header(msg.get("Cc", "")) or None
        payload.reply_to = self._decode_header(msg.get("Reply-To", "")) or None
        payload.sender = self._decode_header(msg.get("Sender", "")) or None
        payload.return_path = msg.get("Return-Path", "") or None
        payload.date = msg.get("Date", "")
        payload.message_id = msg.get("Message-ID", "") or None
        payload.in_reply_to = msg.get("In-Reply-To", "") or None
        payload.references = msg.get("References", "") or None
        payload.priority = self._decode_header(msg.get("X-Priority", "") or msg.get("Importance", "")) or None
        payload.organization = self._decode_header(msg.get("Organization", "")) or None
        payload.x_mailer = msg.get("X-Mailer", "") or None
        payload.delivered_to = msg.get("Delivered-To", "") or None
        payload.x_original_to = msg.get("X-Original-To", "") or None
        payload.authentication_results = msg.get("Authentication-Results", "") or None
        payload.list_id = msg.get("List-Id", "") or None
        payload.list_unsubscribe = msg.get("List-Unsubscribe", "") or None
        payload.content_language = msg.get("Content-Language", "") or None
        payload.disposition_notification_to = msg.get("Disposition-Notification-To", "") or None
        payload.thread_topic = msg.get("Thread-Topic", "") or None
        payload.keywords = msg.get("Keywords", "") or None
        payload.headers = self._collect_headers(msg)

        if msg.is_multipart():
            attachment_idx = 0
            for part in msg.walk():
                content_type = part.get_content_type()

                if content_type == "text/plain":
                    payload.text_body += self._decode_body(part)
                elif content_type == "text/html":
                    payload.html_body += self._decode_body(part)
                elif not part.is_multipart():
                    # 其余内容一律按附件处理:含内联图片 / CID 图片 / PDF 等
                    # (银行账单等常把正文做成内联图片,不处理会丢正文)
                    if not settings.ATTACH:
                        continue
                    filename = part.get_filename()
                    if not filename:
                        attachment_idx += 1
                        filename = self._default_attachment_name(content_type, attachment_idx)
                    self._append_attachment(payload, part, content_type, filename)
        else:
            content_type = msg.get_content_type()
            if content_type == "text/plain":
                payload.text_body = self._decode_body(msg)
            elif content_type == "text/html":
                payload.html_body = self._decode_body(msg)
            elif settings.ATTACH:
                # 整封邮件就是图片 / PDF(无正文文本)
                self._append_attachment(payload, msg, content_type,
                                        self._default_attachment_name(content_type, 1))

        return payload

    @staticmethod
    def _default_attachment_name(content_type: str, idx: int) -> str:
        """内联图片等无文件名内容,按类型生成默认文件名"""
        ext = {
            "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
            "image/webp": "webp", "image/bmp": "bmp", "application/pdf": "pdf",
        }.get(content_type, "bin")
        return f"inline_{idx}.{ext}"

    @staticmethod
    def _fix_surrogates(text: str) -> str:
        """email 解析器会把头部未编码的非 ASCII 原始字节转成 surrogateescape 字符
        (\\udcXX 形式),这类字符会显示乱码、且 JSON 序列化时直接崩溃。
        按 UTF-8 还原为正常文本。"""
        return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")

    @staticmethod
    def _collect_headers(msg) -> dict:
        """全部原始邮件头;重复头(如 Received、DKIM-Signature)用换行拼接保留"""
        headers = {}
        for key, value in msg.items():
            lkey = key.lower()
            # msg.items() 的值可能是 email.header.Header 对象,先转成字符串再清洗
            value = ImapClient._fix_surrogates(str(value))
            headers[lkey] = f"{headers[lkey]}\n{value}" if lkey in headers else value
        return headers

    @staticmethod
    def _decode_header(value) -> str:
        """Decode RFC 2047 encoded-words (e.g. '=?UTF-8?B?...?=') so non-ASCII subjects/senders are readable."""
        if not value:
            return ""
        decoded = []
        for chunk, charset in email.header.decode_header(value):
            if isinstance(chunk, bytes):
                decoded.append(ImapClient._decode_header_bytes(chunk, charset))
            else:
                decoded.append(chunk)
        return ImapClient._fix_surrogates("".join(decoded))

    @staticmethod
    def _decode_header_bytes(chunk: bytes, charset) -> str:
        """带字符集解码头部字节。有些邮件(如腾讯企业邮箱)直接放 GBK 原始字节
        而不是 RFC 2047 编码,所以按 声明charset → UTF-8 → GB18030 依次严格尝试。"""
        candidates = []
        if charset and charset != "unknown-8bit":
            candidates.append(charset)          # 先按声明的真实字符集
        candidates += ["utf-8", "gb18030"]      # 再按 UTF-8 → GB18030 严格尝试
        for cs in candidates:
            try:
                return chunk.decode(cs)
            except (LookupError, UnicodeDecodeError):
                continue
        return chunk.decode("utf-8", errors="replace")

    @staticmethod
    def _decode_body(part) -> str:
        """Decode a MIME part using its declared charset (falls back to UTF-8 for unknown charsets)."""
        data = part.get_payload(decode=True)
        if data is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")

    def _append_attachment(self, payload: MessageEnvelope, part, content_type: str, filename) -> None:
        filename = self._decode_header(filename or "unnamed")
        raw = part.get_payload(decode=True)
        if raw is None:
            logger.warning("Attachment '%s' could not be decoded, skipping", filename)
            return
        if settings.MAX_ATTACH_MB and len(raw) > settings.MAX_ATTACH_MB * 1024 * 1024:
            logger.warning(
                "Attachment '%s' (%s MB) exceeds MAX_ATTACH_MB=%s, skipping",
                filename, len(raw) // (1024 * 1024), settings.MAX_ATTACH_MB,
            )
            return
        payload.attachments.append(Attachment(
            filename=filename,
            content_type=content_type,
            data=base64.b64encode(raw).decode(),
        ))

    # ------------------------------------------------------------------
    # Idle
    # ------------------------------------------------------------------
    def idle(self) -> bool:
        # A fresh tag per command: reusing a tag on the same connection is a
        # protocol violation and some servers reject it.
        tag = self._conn._new_tag()
        logger.debug("Starting IDLE...")
        self._conn.send(f"{tag} IDLE\r\n".encode())

        response = self._conn.readline()
        logger.debug("IDLE confirm: %s", response)
        if not response.startswith(b"+"):
            raise RuntimeError(f"Server rejected IDLE: {response}")

        self._conn.socket().settimeout(self.IDLE_TIMEOUT_SECONDS)
        idle_started = time.monotonic()

        try:
            while True:
                line = self._conn.readline()
                if not line:
                    raise ConnectionError("Server closed the connection")
                logger.debug("IDLE line: %s", line)
                if b"EXISTS" in line:
                    logger.info("New email detected.")
                    self._conn.send(b"DONE\r\n")
                    # Drain the IDLE completion response before issuing new commands
                    while True:
                        line = self._conn.readline()
                        if not line:
                            raise ConnectionError("Server closed the connection")
                        if b"OK" in line or b"NO" in line or b"BAD" in line:
                            break
                    return True
        except Exception as e:
            # Timeout (29 min), server-side close, or interruption: connection state
            # is undefined. Terminate IDLE and drop the connection so the caller
            # reconnects cleanly instead of reusing a broken session.
            logger.warning("IDLE interrupted after %ss: %s",
                           round(time.monotonic() - idle_started, 1), e)
            try:
                self._conn.send(b"DONE\r\n")
            except Exception:
                pass
            self.disconnect()
            return False
