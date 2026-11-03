"""网易大神（ds.163.com）：按作者抓动态。

为什么单独做这个源：
阴阳师的图文攻略主阵地就在这里。B站只能拿标题+视频简介，正文在画面里，
而大神是图文全文，带阵容码、带思路细节，正好补上卡片「打法」那一栏。

可用性（实测）：
- `m.ds.163.com/user/{uid}` 移动版做了服务端渲染，HTML 里直接带 9 条最新动态，
  不需要 Cookie、不需要签名，连续请求 6/6 稳定。
- `m.ds.163.com/feed/{id}/` 详情页同样有 SSR，但正文多为长图，文字量少，
  所以默认只抓列表页拿摘要，不额外打详情页。
- 按关键词搜索、圈子流都返回 404，所以这个源只支持「按作者跟」。

注意：必须用移动端 UA。PC 端 `ds.163.com/user/...` 会 302 跳到移动版，
带着 PC UA 跳过去拿到的是空壳页（没有任何 feed-card）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from yys_core.models import CN_TZ, GuideItem, clean_text, one_line
from yys_fetchers.base import BaseFetcher

# 移动端 UA：这是能拿到 SSR 数据的关键，别改成 PC UA
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_USER_URL = "https://m.ds.163.com/user/{uid}"
# 给用户点开的链接用 PC 站，PC 会自动跳到移动版，两边都能开
_FEED_URL = "https://ds.163.com/feed/{fid}/"

# 一个 feed 卡片的起点。用它切分整页，body 就是到下一个卡片之前的所有内容。
_CARD_SPLIT_RE = re.compile(r'<div id="([0-9a-f]{24})" class="feed-card"')
_AUTHOR_RE = re.compile(r'<div class="user-name[^"]*"><a[^>]*><span>(.*?)</span>', re.S)
_TIME_RE = re.compile(r'<time class="time-location__time">\s*([^<]+?)\s*</time>')
_TEXT_RE = re.compile(r'<div class="feed-card-ellipsis__text feed-text">(.*?)</div>', re.S)
_TOPIC_RE = re.compile(r'<font class="highlight"[^>]*>(#[^<]*?#)</font>')
_COVER_RE = re.compile(r'<img[^>]*src="(https://img\.166\.net/[^"?]+)[^"]*"')
_BR_RE = re.compile(r'<br[^>]*/?\s*>', re.I)

# 「今天09:30」「昨天23:00」「前天12:00」
_REL_DAY_RE = re.compile(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$")
# 「11/1」「10/30」
_MD_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
# 「5分钟前」「3小时前」「2天前」「刚刚」
_AGO_RE = re.compile(r"^(\d+)\s*(秒|分钟|小时|天)前$")
# 「2026-11-01 12:00」或「2026-11-01」；老帖子用的是斜杠「2021/09/24」
_ABS_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$")

_REL_DAYS = {"今天": 0, "昨天": 1, "前天": 2}
_AGO_UNITS = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}


def parse_ds_time(raw: str, now: datetime | None = None) -> datetime | None:
    """解析大神的时间文案。

    页面上只有相对时间，没有绝对时间戳，所以必须自己推算：
    「刚刚」「5分钟前」「3小时前」「今天09:30」「昨天23:00」「前天12:00」「11/1」

    注意两种绝对日期都出现过：新帖子是「2026-11-01」，2021 年前后的老帖子
    是斜杠「2021/09/24」，不认斜杠的话停更号的时间会全是 None。

    「11/1」这种月/日最难：跨年时得减一年。
    如果算出来的日期比现在还晚，说明是去年的（比如 1 月看到「12/30」）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)

    if text in ("刚刚", "现在"):
        return now

    m = _AGO_RE.match(text)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _AGO_UNITS[m.group(2)])

    m = _REL_DAY_RE.match(text)
    if m:
        days = _REL_DAYS[m.group(1)]
        day = (now - timedelta(days=days)).date()
        return datetime(
            day.year, day.month, day.day,
            int(m.group(2)), int(m.group(3)), tzinfo=CN_TZ,
        )

    m = _ABS_RE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4) or 0)
        minute = int(m.group(5) or 0)
        try:
            return datetime(year, month, day, hour, minute, tzinfo=CN_TZ)
        except ValueError:
            return None

    m = _MD_RE.match(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            guess = datetime(now.year, month, day, tzinfo=CN_TZ)
        except ValueError:
            return None
        # 比现在还晚 → 一定是去年的（1 月看到 12/30）
        if guess > now + timedelta(days=1):
            try:
                guess = guess.replace(year=now.year - 1)
            except ValueError:
                return None
        return guess

    if text in ("昨天", "前天", "今天"):
        days = _REL_DAYS[text]
        return datetime.combine(
            (now - timedelta(days=days)).date(), datetime.min.time(), tzinfo=CN_TZ
        )

    return None


def _split_title(text: str, topics: list[str]) -> tuple[str, str]:
    """把一段动态正文拆成「标题 + 摘要」。

    大神的动态没有独立标题字段，惯例是第一行当标题，
    所以取首行（太短就多取一行），剩下的当摘要。
    """
    body = text
    for topic in topics:
        body = body.replace(topic, " ")
    lines = [one_line(line) for line in body.split("\n")]
    lines = [line for line in lines if line and line not in ("<查看更多>", "查看更多")]

    if not lines:
        return (topics[0].strip("#") if topics else "无标题"), ""

    title = lines[0]
    rest_start = 1
    # 首行太短（比如只有「【攻略】」）就把第二行也拼进标题
    if len(title) < 8 and len(lines) > 1:
        title = f"{title} {lines[1]}"
        rest_start = 2

    digest = one_line(" ".join(lines[rest_start:]))
    return title[:80], digest


class Ds163Fetcher(BaseFetcher):
    """网易大神。目前只支持按作者抓，没有关键词搜索能力。"""

    source = "ds163"
    # 详情页正文多为长图，补抓收益低，关掉省请求
    enrich_limit = 0

    async def fetch_author(self, target: dict[str, Any], limit: int) -> list[GuideItem]:
        uid = str(target.get("uid") or target.get("mid") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", uid):
            logger.warning(
                f"[yys-guide] 大神 uid 无效：{uid!r}（应为 32 位十六进制，"
                f"来自 m.ds.163.com/user/<uid>），已跳过"
            )
            return []
        label = target.get("name") or uid

        try:
            html = await self.client.get_text(
                _USER_URL.format(uid=uid),
                headers={"User-Agent": _MOBILE_UA, "Referer": "https://m.ds.163.com/"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] 大神用户页抓取失败 {label}：{exc!r}")
            return []

        items = self.parse_user_page(html, uid)
        if not items:
            # 页面能打开但解析不出卡片，多半是页面结构变了，务必把原文长度打出来
            logger.warning(
                f"[yys-guide] 大神用户页没解析到动态 {label}（HTML {len(html)} 字符），"
                f"可能是页面结构变更"
            )
            return []

        topics_filter = str(target.get("topics") or "").strip()
        if topics_filter:
            wanted = [w.strip() for w in topics_filter.split(",") if w.strip()]
            items = [it for it in items if self._match_topics(it, wanted)]
            if not items:
                logger.debug(f"[yys-guide] 大神博主 {label} 近期内容没命中话题：{wanted}")

        items.sort(key=lambda item: item.published_ts, reverse=True)
        return items[: max(1, limit)]

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        """大神没有可用的搜索页（实测 404），这个源只按作者跟。"""
        logger.debug(f"[yys-guide] 大神不支持关键词搜索，跳过「{keyword}」")
        return []

    @staticmethod
    def _match_topics(item: GuideItem, wanted: list[str]) -> bool:
        haystack = f"{item.title} {item.digest} {' '.join(item.meta.get('topics') or [])}"
        return any(word.lower() in haystack.lower() for word in wanted)

    def parse_user_page(self, html: str, uid: str) -> list[GuideItem]:
        """从用户主页 SSR 出来的 HTML 里拆出每一条动态。"""
        parts = _CARD_SPLIT_RE.split(html)
        # split 结果：[页面前半段, fid1, body1, fid2, body2, ...]
        items: list[GuideItem] = []
        now = datetime.now(CN_TZ)

        for index in range(1, len(parts) - 1, 2):
            fid = parts[index]
            body = parts[index + 1]

            author_m = _AUTHOR_RE.search(body)
            author = clean_text(author_m.group(1)) if author_m else ""
            time_m = _TIME_RE.search(body)
            published = parse_ds_time(time_m.group(1), now) if time_m else None

            text_m = _TEXT_RE.search(body)
            if text_m:
                raw = _BR_RE.sub("\n", text_m.group(1))
                text = clean_text(raw)
            else:
                text = ""
            # 视频类动态可能没有文字，只有标题在别处，跳过即可
            if not text:
                continue

            topics = [clean_text(t) for t in _TOPIC_RE.findall(body)]
            title, digest = _split_title(text, topics)
            if not title:
                continue

            cover_m = _COVER_RE.search(body)
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=fid,
                    title=title,
                    url=_FEED_URL.format(fid=fid),
                    author=author,
                    published=published,
                    digest=digest,
                    cover=cover_m.group(1) if cover_m else "",
                    meta={
                        "type": "feed",
                        "uid": uid,
                        "topics": topics,
                        "chars": len(text),
                    },
                )
            )

        return items
