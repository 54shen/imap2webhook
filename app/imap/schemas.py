from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

class Attachment(BaseModel):
    filename:     str
    content_type: str
    data:         str

class MessageEnvelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uid:            str
    account:        str = 'default'   # 来源账户名(多账户时用 .env 前缀编号区分)
    subject:        Optional[str] = ''
    from_:          Optional[str] = Field(None, alias="from")
    to:             Optional[str] = None
    cc:             Optional[str] = None
    reply_to:       Optional[str] = None
    sender:         Optional[str] = None
    return_path:    Optional[str] = None
    date:           Optional[str] = None
    message_id:     Optional[str] = None
    in_reply_to:    Optional[str] = None
    references:     Optional[str] = None
    priority:       Optional[str] = None
    organization:   Optional[str] = None
    x_mailer:       Optional[str] = None
    delivered_to:   Optional[str] = None
    x_original_to:  Optional[str] = None
    authentication_results: Optional[str] = None
    list_id:        Optional[str] = None
    list_unsubscribe: Optional[str] = None
    content_language: Optional[str] = None
    disposition_notification_to: Optional[str] = None
    thread_topic:   Optional[str] = None
    keywords:       Optional[str] = None
    text_body:      Optional[str] = ''
    html_body:      Optional[str] = ''
    attachments:    list[Attachment] = []
    headers:        dict[str, str] = {}   # 全部原始邮件头,重复头用换行拼接
