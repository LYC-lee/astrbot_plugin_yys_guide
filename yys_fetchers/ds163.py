"""网易大神（ds.163.com）：按作者抓动态，支持翻页翻历史。

为什么单独做这个源：
阴阳师的图文攻略主阵地就在这里。B站只能拿标题+视频简介，正文在画面里，
而大神是图文全文，带阵容码、带思路细节，正好补上卡片「打法」那一栏。

两条通道（实测）：
1. JSON 接口（主力）：`POST https://god.gameyw.netease.com/v1/app/feed/page`
   体 `{"feedUid": <uid>, "count": 50}`，回 50 条完整动态（含正文与毫秒时间戳）。
   `count` 上限就是 50；再多也只会返回 50 条。
   翻页靠 `maxTime`（传上一页最后一条的 createTime），能一页页翻到历史深处。
   这个 host 是从站点 bundle 里反查出来的：`/v1/...` 的 baseURL 是
   `https://god<l>.gameyw.netease.com`，`<l>` 对 `app/ad/web/...` 前缀为空。
2. HTML SSR（兜底）：`m.ds.163.com/user/{uid}` 服务端渲染出**最新 9 条**，
   不需要 Cookie、不需要签名。接口哪天挂了就退回这里，至少还有 9 条。

注意：HTML 通道必须用移动端 UA。PC 端 `ds.163.com/user/...` 会 302 跳到移动版，
带着 PC UA 跳过去拿到的是空壳页（没有任何 feed-card）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from yys_core.models import CN_TZ, GuideItem, clean_text, one_line
from yys_fetchers.base import BaseFetcher

# 移动端 UA：HTML 通道能拿到 SSR 数据的关键，别改成 PC UA
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_USER_URL = "https://m.ds.163.com/user/{uid}"
# 给用户点开的链接用 PC 站，PC 会自动跳到移动版，两边都能开
_FEED_URL = "https://ds.163.com/feed/{fid}/"

# JSON 接口（翻页靠 maxTime，见模块注释）
_API_URL = "https://god.gameyw.netease.com/v1/app/feed/page"
_API_HEADERS = {
    "User-Agent": _MOBILE_UA,
    "Referer": "https://m.ds.163.com/",
    "Origin": "https://m.ds.163.com",
    "Content-Type": "application/json",
}
# count 到 50 就是上限，再大也只给 50，所以第一页一次拿满
_FIRST_PAGE = 50
_NEXT_PAGE = 20
# 翻页之间的间隔，连着打 5 次接口容易被风控
_PAGE_GAP = 0.25

# 一个 feed 卡片的起点。用它切分整页，body 就是到下一个卡片之前的所有内容。
_CARD_SPLIT_RE = re.compile(r'<div id="([0-9a-f]{24})" class="feed-card"')
_AUTHOR_RE = re.compile(r'<div class="user-name[^"]*"><a[^>]*><span>(.*?)</span>', re.S)
_TIME_RE = re.compile(r'<time class="time-location__time">\s*([^<]+?)\s*</time>')
_TEXT_RE = re.compile(r'<div class="feed-card-ellipsis__text feed-text">(.*?)</div>', re.S)
_TOPIC_RE = re.compile(r'<font class="highlight"[^>]*>(#[^<]*?#)</font>')
_COVER_RE = re.compile(r'<img[^>]*src="(https://img\.166\.net/[^"?]+)[^"]*"')
# 同一篇图文里常有好几张图，阵容二维码通常就在其中一张上。
# 全部留下（封顶 6 张），交给上层去解码——只看封面会漏掉真正的攻略图。
_IMAGES_RE = _COVER_RE
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
    """网易大神。按作者抓，能翻历史，不能按关键词搜（搜索页 404）。"""

    source = "ds163"
    # 正文已经随接口一起回来了，不需要再补抓详情页
    enrich_limit = 0

    async def fetch_author(
        self,
        target: dict[str, Any],
        limit: int,
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        """抓某个博主的内容。

        给了 keywords 就进「翻历史」模式：一页页往回翻，边翻边在标题里找，
        凑够 limit 条命中的就停。没给就只取最新一页（够卡片用就行，省流量）。

        每次都先试 JSON 接口；接口整体不可用时才退回 HTML 的 9 条。
        """
        uid = str(target.get("uid") or target.get("mid") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", uid):
            logger.warning(
                f"[yys-guide] 大神 uid 无效：{uid!r}（应为 32 位十六进制，"
                f"来自 m.ds.163.com/user/<uid>），已跳过"
            )
            return []
        label = target.get("name") or uid

        pages = self._scan_pages() if keywords else 1
        items = await self._fetch_api(uid, limit, keywords, pages, label)
        if items is None:
            logger.debug(f"[yys-guide] 大神接口不可用，{label} 退回 HTML 通道")
            items = await self._fetch_html(uid, label)
            # HTML 通道只有 9 条，且拿不到更早的，明确留个记录
            if items and keywords:
                logger.debug(
                    f"[yys-guide] {label} 只有 HTML 的 {len(items)} 条，"
                    "翻不了历史，可能漏掉较早的相关内容"
                )

        if not items:
            return []

        topics_filter = str(target.get("topics") or "").strip()
        if topics_filter:
            wanted = [w.strip() for w in topics_filter.split(",") if w.strip()]
            items = [it for it in items if self._match_topics(it, wanted)]
            if not items:
                logger.debug(f"[yys-guide] 大神博主 {label} 近期内容没命中话题：{wanted}")

        items.sort(key=lambda item: item.published_ts, reverse=True)
        if keywords:
            # 命中的排前面：limit 是给卡片的上限，别让时间最新的无关内容挤掉对题的
            words = [w.lower() for w in keywords if w]
            items.sort(
                key=lambda it: (
                    0
                    if any(w in it.title.lower() for w in words)
                    else 1,
                    -it.published_ts,
                )
            )
        return items[: max(1, limit)]

    # -------------------------------------------------- JSON 通道（可翻页）

    async def _fetch_api(
        self,
        uid: str,
        limit: int,
        keywords: list[str] | None,
        pages: int,
        label: str,
    ) -> list[GuideItem] | None:
        """返回 None 表示接口不可用（让上层退回 HTML），返回 [] 表示可用但没内容。"""
        items: list[GuideItem] = []
        seen: set[str] = set()
        max_time: int | None = None
        usable = False
        words = [w.lower() for w in (keywords or []) if w]
        deadline = time.time() - self._scan_days() * 86400 if words else 0
        # 命中目标别设太高：卡片最多 5 条，从 8 个博主各拿 3 条已经远远够用。
        # 设成 limit（12）会逼着每个博主都往回翻好几页，白等十几秒。
        goal = min(max(1, limit), 3) if words else 0

        for page in range(max(1, pages)):
            payload: dict[str, Any] = {
                "feedUid": uid,
                "count": _FIRST_PAGE if page == 0 else _NEXT_PAGE,
            }
            if max_time:
                payload["maxTime"] = max_time

            try:
                data = await self.client.post_json(
                    _API_URL, payload, headers=_API_HEADERS
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 大神接口请求失败 {label}：{exc!r}")
                break

            if not isinstance(data, dict) or data.get("code") != 200:
                logger.debug(
                    f"[yys-guide] 大神接口返回异常 {label}："
                    f"{str(data)[:120] if isinstance(data, dict) else type(data).__name__}"
                )
                break

            usable = True
            feeds = (data.get("result") or {}).get("feeds") or []
            if not feeds:
                break

            for feed in feeds:
                item = self.parse_api_feed(feed, uid)
                if item and item.item_id not in seen:
                    seen.add(item.item_id)
                    items.append(item)

            # 没有关键词时只取第一页，50 条已经远超卡片需求
            if not words:
                break
            # 命中的够数了就不用再往回翻
            if sum(1 for it in items if any(w in it.title.lower() for w in words)) >= goal:
                break

            oldest = int(feeds[-1].get("createTime") or 0)
            if not oldest:
                break
            # 翻到时间窗口之外就停：再往前的内容太旧，给了也没人看
            if deadline and oldest / 1000 < deadline:
                break
            max_time = oldest
            if page + 1 < pages:
                await asyncio.sleep(_PAGE_GAP)

        return items if usable else None

    def parse_api_feed(self, feed: dict[str, Any], uid: str) -> GuideItem | None:
        """把接口返回的一条动态转成 GuideItem。"""
        fid = str(feed.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{24}", fid):
            return None

        content = feed.get("feedContent")
        if not isinstance(content, dict):
            try:
                content = json.loads(feed.get("content") or "{}")
            except (ValueError, TypeError):
                content = {}
        body = content.get("body") or {}
        # 长文（type=6）自带 title，正文首行不是标题；
        # 普通动态没有 title 字段，只能按惯例拿首行当标题
        head_title = str(body.get("title") or "").strip()

        raw_text = ""
        for key in ("text", "summary", "digest"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                raw_text = value
                break
        if not raw_text and not head_title:
            return None

        text = clean_text(_BR_RE.sub("\n", raw_text)) if raw_text else ""

        topics = [
            str(t.get("topicName") or "").strip()
            for t in (feed.get("topicInfoList") or [])
            if isinstance(t, dict)
        ]
        topics = [t for t in topics if t]

        if head_title:
            title = one_line(head_title)[:80]
            digest = text
        else:
            # 这里不传话题给 _split_title：接口给的 topicName 不带井号，
            # 直接 replace 会把正文里的「斗技」这类正常词也抹掉
            title, digest = _split_title(text, [])
        if not title:
            return None

        created = int(feed.get("createTime") or 0)
        published = (
            datetime.fromtimestamp(created / 1000, CN_TZ) if created > 0 else None
        )

        # 正文配图在 body.media 里。攻略「一图流」就是一张 1700×5900 的长图，
        # 阵容二维码压在图上某个角落——只看文字是拿不到码的。
        media = body.get("media") or []
        images = [
            str(entry.get("url") or "").strip()
            for entry in media
            if isinstance(entry, dict)
        ]
        images = [url for url in images if url.startswith("http")][:6]

        return GuideItem(
            source=self.source,
            item_id=fid,
            title=title,
            url=_FEED_URL.format(fid=fid),
            author=self._feed_author(feed),
            published=published,
            digest=digest,
            cover=images[0] if images else "",
            meta={
                "type": "feed",
                "uid": uid,
                "topics": topics,
                "chars": len(text),
                "kind": feed.get("type"),
                "images": images,
            },
        )

    @staticmethod
    def _feed_author(feed: dict[str, Any]) -> str:
        """作者昵称藏在 appRole 的 JSON 字符串里。"""
        role = feed.get("appRole")
        if isinstance(role, str):
            try:
                role = json.loads(role)
            except (ValueError, TypeError):
                role = None
        if isinstance(role, dict):
            nick = str(role.get("nick") or "").strip()
            if nick:
                return nick
        return ""

    # -------------------------------------------------- HTML 通道（兜底 9 条）

    async def _fetch_html(self, uid: str, label: str) -> list[GuideItem]:
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
            logger.warning(
                f"[yys-guide] 大神用户页没解析到动态 {label}（HTML {len(html)} 字符），"
                f"可能是页面结构变更"
            )
        return items

    def _scan_pages(self) -> int:
        try:
            return max(1, int(self.conf.get("ds163_scan_pages", 4) or 4))
        except (TypeError, ValueError):
            return 4

    def _scan_days(self) -> int:
        try:
            return max(0, int(self.conf.get("ds163_scan_days", 180) or 0))
        except (TypeError, ValueError):
            return 180

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

            images = _IMAGES_RE.findall(body)[:6]
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=fid,
                    title=title,
                    url=_FEED_URL.format(fid=fid),
                    author=author,
                    published=published,
                    digest=digest,
                    cover=images[0] if images else "",
                    meta={
                        "type": "feed",
                        "uid": uid,
                        "topics": topics,
                        "chars": len(text),
                        # 留给阵容二维码解码用：原文里的图比封面更可能带码
                        "images": images,
                    },
                )
            )

        return items
