#!/usr/bin/env python
"""
把邮件 HTML 正文整体渲染成 PNG 图片。
段落、标题、列表、表格、网页图片按阅读顺序排版到一张图上,适合微信等
不支持 markdown 的渠道。用法:
    png_bytes = render_body_image(html)   # 无内容/失败返回 None
"""
import base64
import html.parser
import io
import re

import requests
from PIL import Image, ImageDraw, ImageFont

# 中文字体候选(按平台顺序查找,找不到就用默认字体)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",                       # Windows 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",                     # Windows 黑体
    r"C:\Windows\Fonts\simsun.ttc",                     # Windows 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux Noto CJK
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",   # Linux 文泉驿微米黑
]
FONT_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",                     # 微软雅黑 Bold
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]

_font_cache, _font_bold_cache = {}, {}


def _load_font(size: int, bold: bool = False):
    cache = _font_bold_cache if bold else _font_cache
    if size not in cache:
        for path in (FONT_BOLD_CANDIDATES if bold else FONT_CANDIDATES):
            try:
                cache[size] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            cache[size] = ImageFont.load_default()
    return cache[size]


# ---------------------------------------------------------------- 表格提取

class _TableExtractor(html.parser.HTMLParser):
    """从 HTML 中提取表格:表 → 行 → 单元格文本"""
    SKIP_TAGS = {"style", "script", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip += 1
        if self.skip:
            return
        if tag == "tr" and self.row is None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None and self.cell is None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        if self.skip:
            return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(_clean("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if not self.skip and self.cell is not None:
            self.cell.append(data)


# ---------------------------------------------------------------- 正文流提取

TABLE_MARKER = re.compile(r"\x00T(\d+)\x00")


def _split_tables(html: str) -> tuple[str, list[list[list[str]]]]:
    """把 <table> 整块换成占位符,表格内容单独提取(支持一行一表的账单邮件)"""
    tables = []

    def repl(m):
        idx = len(tables)
        parser = _TableExtractor()
        parser.feed(m.group(0))
        tables.append(parser.rows)
        return f"\x00T{idx}\x00"

    modified = re.sub(r"<table[^>]*>.*?</table>", repl, html, flags=re.S | re.I)
    return modified, tables


class _FlowExtractor(html.parser.HTMLParser):
    """按阅读顺序提取正文:段落/标题/列表项/分隔线/图片,表格位置留占位符"""
    # 注意:不要放 meta/link——它们是 HTML4 自闭合标签(无结束标签),
    # 放进 SKIP_TAGS 会让 skip 计数永远回不到 0,正文被静默丢弃(踩过两次坑)
    SKIP_TAGS = {"style", "script", "head", "title", "noscript"}
    HEADING_SIZE = {"h1": 22, "h2": 20, "h3": 18, "h4": 16, "h5": 15, "h6": 15}
    BODY_SIZE = 14
    LINK_COLOR = (0, 91, 197)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []          # {"type": text/list_item/rule/image, ...}
        self.skip = 0
        self.spans = []           # 当前块 [(text, bold, color)]
        self.block_type = None    # text / list_item
        self.heading_size = 0
        self.bold = 0
        self.link = 0

    # ---- 块管理 ----
    def _close_block(self):
        if self.spans:
            text = "".join(t for t, _, _ in self.spans).strip()
            if text:
                self.blocks.append({
                    "type": "list_item" if self.block_type == "list_item" else "text",
                    "text": text,
                    "size": self.heading_size or self.BODY_SIZE,
                    "bold": bool(self.heading_size),
                })
        self.spans, self.block_type, self.heading_size = [], None, 0

    def _start_block(self, block_type="text", heading=0):
        self._close_block()
        self.block_type = block_type
        self.heading_size = heading

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("p", "div", "section", "article", "blockquote", "tr", "td", "th"):
            self._start_block()
        elif tag == "li":
            self._start_block("list_item")
        elif tag == "br":
            self.spans.append(("\n", False, None))
        elif tag in self.HEADING_SIZE:
            self._start_block("text", self.HEADING_SIZE[tag])
        elif tag == "hr":
            self._close_block()
            self.blocks.append({"type": "rule"})
        elif tag in ("b", "strong"):
            self.bold += 1
        elif tag == "a":
            self.link += 1
        elif tag == "img":
            self._start_block("image")
            attrs_d = dict(attrs)
            self.blocks.append({"type": "image", "src": attrs_d.get("src", ""),
                                "alt": attrs_d.get("alt", "")})
            self._start_block()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("p", "div", "section", "article", "blockquote", "tr", "td", "th", "li"):
            self._close_block()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._close_block()
        elif tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif tag == "a":
            self.link = max(0, self.link - 1)

    def handle_data(self, data):
        if self.skip:
            return
        color = self.LINK_COLOR if self.link else None
        self.spans.append((data, self.bold > 0, color))


def _clean(text: str) -> str:
    return " ".join(text.split()).strip()


# ---------------------------------------------------------------- 图片获取

def _fetch_image(src: str, timeout: int = 5, max_bytes: int = 3 * 1024 * 1024):
    """加载 HTML 里的图片(data: URI 或 http(s) URL),失败返回 None"""
    try:
        if src.startswith("data:image"):
            raw = base64.b64decode(src.split(",", 1)[1])
        else:
            resp = requests.get(src, timeout=timeout)
            if resp.status_code != 200:
                return None
            raw = resp.content
        if not raw or len(raw) > max_bytes:
            return None
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------- 渲染

def render_body_image(html: str, max_width: int = 800, max_height: int = 3000) -> bytes | None:
    """把 HTML 正文整体渲染成 PNG(段落/列表/表格/图片按阅读顺序排版)"""
    modified, tables = _split_tables(html)
    flow = _FlowExtractor()
    try:
        flow.feed(modified)
    except Exception:
        return None

    # 展开占位符,得到有序块列表
    blocks = []
    for b in flow.blocks:
        if b["type"] in ("text", "list_item"):
            parts = TABLE_MARKER.split(b["text"])
            for i, part in enumerate(parts):
                if i % 2 == 1:
                    idx = int(part)
                    if idx < len(tables) and tables[idx]:
                        blocks.append({"type": "table", "rows": tables[idx]})
                elif part.strip():
                    nb = dict(b)
                    nb["text"] = part.strip()
                    blocks.append(nb)
        else:
            blocks.append(b)
    if not blocks:
        return None

    margin = 20
    content_w = max_width - margin * 2
    gap = 8
    pad_x, pad_y = 8, 5
    cell_max_w = 320

    def wrap(text: str, width: int, font) -> list[str]:
        lines, cur = [], ""
        for ch in text:
            if ch == "\n":
                if cur:
                    lines.append(cur)
                    cur = ""
                continue
            if cur and font.getlength(cur + ch) > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines or [""]

    # 第一遍:计算每个块的高度与布局数据
    laid = []          # (type, height, data)
    for b in blocks:
        if b["type"] in ("text", "list_item"):
            size, bold = b["size"], b["bold"]
            font = _load_font(size, bold)
            prefix = "• " if b["type"] == "list_item" else ""
            text = prefix + b["text"]
            lines = wrap(text, content_w, font)
            h = len(lines) * (size + 8)
            laid.append(("text", h, (lines, font, size)))
        elif b["type"] == "table":
            rows = b["rows"]
            ncols = max(len(r) for r in rows)
            col_has = [any(r[c].strip() for r in rows if c < len(r)) for c in range(ncols)]
            clean_rows = []
            for r in rows:
                cells = [r[c] for c in range(ncols) if col_has[c] and c < len(r)]
                while cells and not cells[-1].strip():
                    cells.pop()
                while cells and not cells[0].strip():
                    cells.pop(0)
                if any(x.strip() for x in cells):
                    clean_rows.append(cells)
            if not clean_rows:
                continue
            font = _load_font(13)
            mcols = max(len(r) for r in clean_rows)
            widths = [int(min(cell_max_w, max((font.getlength(r[c]) for r in clean_rows if c < len(r)), default=0))) + pad_x * 2
                      for c in range(mcols)]
            if sum(widths) > content_w:          # 总宽超限则整体压缩
                k = content_w / sum(widths)
                widths = [max(40, int(w * k)) for w in widths]
            wrapped = [[wrap(r[c], widths[c] - pad_x * 2, font) for c in range(len(r))] for r in clean_rows]
            heights = [max(len(ls) for ls in row) * 19 + pad_y * 2 for row in wrapped]
            laid.append(("table", sum(heights) + len(heights) + 1, (widths, wrapped, heights)))
        elif b["type"] == "image":
            img = _fetch_image(b["src"])
            if img is None:
                continue
            w, h = img.size
            k = min(1.0, content_w / w)
            if k < 1.0:
                img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
            laid.append(("image", img.size[1], img))
        elif b["type"] == "rule":
            laid.append(("rule", 3, None))

    if not laid:
        return None
    total_h = sum(h for _, h, _ in laid) + gap * (len(laid) - 1)
    scale = min(1.0, max_height / total_h if total_h > max_height else 1.0)

    img = Image.new("RGB", (max_width, int(total_h * scale)), "white")
    draw = ImageDraw.Draw(img)
    y = 0
    for kind, h, data in laid:
        if kind == "text":
            lines, font, size = data
            ty = y
            for line in lines:
                draw.text((margin, ty), line, fill="black", font=font)
                ty += size + 8
        elif kind == "table":
            widths, wrapped, heights = data
            block_w = sum(widths)
            x0 = margin + (content_w - block_w) // 2
            ty = y
            for ri, row in enumerate(wrapped):
                rh = heights[ri]
                x = x0
                for ci, lines in enumerate(row):
                    cw = widths[ci]
                    if ri == 0:
                        draw.rectangle([x, ty, x + cw, ty + rh], fill="#f0f0f0")
                    tty = ty + pad_y
                    for line in lines:
                        draw.text((x + pad_x, tty), line, fill="black", font=_load_font(13))
                        tty += 19
                    x += cw
                draw.line([x0, ty, x0 + block_w, ty], fill="#cccccc")
                ty += rh
            draw.line([x0, ty, x0 + block_w, ty], fill="#cccccc")
        elif kind == "image":
            draw.image(data, (margin, y))
        elif kind == "rule":
            draw.line([margin, y + 1, max_width - margin, y + 1], fill="#bbbbbb")
        y += int(h * scale) + gap

    if scale < 1.0:
        img = img.resize((max_width, int(total_h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
