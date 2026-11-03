"""统一的数据模型：所有抓取器（fetcher）最终都产出 GuideItem，渲染层与摘要层只认它。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# 国内用户，统一按 UTC+8 展示
CN_TZ = timezone(timedelta(hours=8))

_WS_RE = re.compile(r"[ \t\u3000]+")
# 块级标签先换成空格，否则「第一段<br>第二段」会粘在一起
_BLOCK_TAG_RE = re.compile(r"<\s*(?:br|/p|/div|/h[1-6]|/li|/tr)\s*/?\s*>", re.I)
# 其余行内标签直接删掉。B站搜索结果的 <em> 高亮如果换成空格，
# 标题会变成「【 阴阳师 调号】」这种多余空格的怪样子
_TAG_RE = re.compile(r"<[^>]+>")
_EMPTY_LINE_RE = re.compile(r"\n{3,}")

_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "—",
    "&hellip;": "…",
}


def clean_text(raw: str | None) -> str:
    """去掉 HTML 标签、实体、B站搜索结果的 <em> 高亮，压平空白。"""
    if not raw:
        return ""
    text = _BLOCK_TAG_RE.sub(" ", str(raw))
    text = _TAG_RE.sub("", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _EMPTY_LINE_RE.sub("\n\n", text).strip()


def one_line(raw: str | None, limit: int = 0) -> str:
    """压成单行，用于卡片里的字段，避免换行把排版撑坏。"""
    text = " ".join((raw or "").split())
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# 标题开头的【标签】：「【秘闻副本】荒川百战」-> 「荒川百战」
_TITLE_TAG_RE = re.compile(r"^[【\[（(][^】\]）)]{1,20}[】\]）)]\s*")
# 同一篇攻略换个发布形态会改标题，比较前先抹掉这些词：
# 「飞羽凌云图片版：三套阵容」和「飞羽凌云：三套阵容」说的是同一条
_TITLE_FORM_RE = re.compile(r"(图片版|视频版|图文版|文字版|高清版|横版|竖版|完整版|修正版|一图流)")
_TITLE_PUNCT_RE = re.compile(r"[\s:：,，。.!！?？·\-—_+~～/|、]+")


def title_core(title: str) -> str:
    """去掉开头【标签】，留下真正在说事的标题。"""
    return _TITLE_TAG_RE.sub("", title or "").strip()


def title_key(title: str) -> str:
    """比较用的归一化标题：去标签、去形态词、去标点空白、转小写。"""
    text = _TITLE_FORM_RE.sub("", title_core(title))
    return _TITLE_PUNCT_RE.sub("", text).lower()


def fmt_human_time(published: datetime | None, now: datetime | None = None) -> str:
    """把时间转成「2 小时前」这种人类可读形式。"""
    if not published:
        return "时间未知"
    if published.tzinfo is None:
        published = published.replace(tzinfo=CN_TZ)
    now = now or datetime.now(CN_TZ)
    delta = now - published.astimezone(CN_TZ)
    seconds = delta.total_seconds()

    if seconds < 0:
        return published.astimezone(CN_TZ).strftime("%m-%d %H:%M")
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 3:
        return f"{int(seconds // 86400)} 天前"
    return published.astimezone(CN_TZ).strftime("%m-%d %H:%M")


@dataclass
class GuideItem:
    """一条攻略。source + item_id 唯一确定它。"""

    source: str
    item_id: str
    title: str
    url: str
    author: str = ""
    published: datetime | None = None
    digest: str = ""
    content: str = ""
    cover: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.item_id}"

    @property
    def published_ts(self) -> float:
        if not self.published:
            return 0.0
        dt = self.published
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt.timestamp()

    @property
    def text_for_llm(self) -> str:
        parts = [self.title]
        if self.digest:
            parts.append(self.digest)
        if self.content and self.content not in self.digest:
            parts.append(self.content)
        return "\n".join(p for p in parts if p)

    def dedup_hash(self) -> str:
        base = f"{self.source}|{clean_text(self.title)}".lower()
        return hashlib.md5(base.encode("utf-8")).hexdigest()

    def to_row(self) -> tuple:
        published = ""
        if self.published:
            dt = self.published
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            published = dt.astimezone(CN_TZ).isoformat()
        import json

        return (
            self.source,
            self.item_id,
            self.title,
            self.url,
            self.author,
            published,
            self.digest,
            self.content,
            self.cover,
            json.dumps(self.meta, ensure_ascii=False),
        )

    @classmethod
    def from_row(cls, row: tuple) -> "GuideItem":
        import json

        (
            source,
            item_id,
            title,
            url,
            author,
            published,
            digest,
            content,
            cover,
            meta_json,
        ) = row
        published_dt = None
        if published:
            try:
                published_dt = datetime.fromisoformat(published)
            except ValueError:
                published_dt = None
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except (ValueError, TypeError):
            meta = {}
        return cls(
            source=source,
            item_id=item_id,
            title=title,
            url=url,
            author=author or "",
            published=published_dt,
            digest=digest or "",
            content=content or "",
            cover=cover or "",
            meta=meta,
        )


# 平台 id -> 展示名
SOURCE_LABELS = {
    "bilibili": "B站",
    "ds163": "大神",
    "nga": "NGA",
    "weibo": "微博",
    "douyin": "抖音",
    "web": "网页",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)
