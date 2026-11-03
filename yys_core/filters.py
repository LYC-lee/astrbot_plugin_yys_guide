"""内容过滤。

B站搜「阴阳师 御魂」，第一页常年混着「满级御魂8400+，精调方案，私信优惠」
这类代练广告。不挡掉的话，真正有用的攻略会被挤出卡片。

只挡强信号的营销内容，宁可漏也不能误伤正常攻略。
"""

from __future__ import annotations

from yys_core.models import GuideItem

#: 命中任意一个就判定为营销内容
SPAM_KEYWORDS: tuple[str, ...] = (
    "代练", "代肝", "代打", "代刷",
    "调号",          # B站黑话，指收费帮人调账号配置
    "买号", "卖号", "租号", "挂号",
    "无限内购", "不限内购", "内购破解", "GM后台", "gm后台",
    "破解版", "辅助脚本", "自动脚本",
    "低价代", "最优惠价格",
)

#: 需要同时出现才判定的组合，降低误伤
SPAM_PAIRS: tuple[tuple[str, str], ...] = (
    ("私信", "价格"),
    ("私信", "优惠"),   # 「满级御魂8400+，精调方案，私信优惠」这类调号广告
    ("加群", "优惠"),
    ("包赔", "账号"),
)


def is_spam(*texts: str | None) -> bool:
    blob = " ".join(text for text in texts if text).lower()
    if not blob:
        return False
    if any(word.lower() in blob for word in SPAM_KEYWORDS):
        return True
    return any(first in blob and second in blob for first, second in SPAM_PAIRS)


#: 搜索结果标题里被混进来的站点标识。
#: 站点自己接口返回的标题是干净的，带这些的只可能来自搜索引擎抓 HTML，
#: 属于典型的关键词堆砌落地页。
_SITE_TAILS: tuple[str, ...] = (
    "bilibili",
    "哔哩哔哩",
    "_知乎",
    "_百度",
    "_搜狐",
    "_新浪",
    "_网易",
    "_抖音",
    "csdn",
    "_贴吧",
)


def is_seo_junk(title: str | None) -> bool:
    """搜索引擎结果里那种堆砌关键词的落地页。

    典型长相：
    「阴阳师:当前版本道馆突破阵容教学 _《阴阳师》手游官网 《阴阳师》手游官网_
      唯美奇幻 匠心巨制,开启唯美奇幻之旅」
    「【阴阳师】道馆阵容?看这一个视频就够了!_哔哩哔哩bilibili_阴阳师」

    这类页面标题把站点名和 slogan 全塞进去，点开通常也没有真内容。
    判据都取强信号，避免误伤正常标题——比如
    「【阴阳师】咲耶&ur大天狗版本最新道馆进攻阵容（附图文方案码）」不该被拦。
    """
    text = (title or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "手游官网" in text or "手机游戏" in text or "手游网" in text:
        return True
    if "_《" in text and "官网" in text:
        return True
    if any(tail in lowered for tail in _SITE_TAILS):
        return True
    # 门户站的搜索结果标题习惯用双下划线接站点名：
    # 「阴阳师道馆什么阵容强势道馆强势阵容推荐__九游手机游戏」
    # 正常攻略标题里基本不会出现连续两个下划线
    if "__" in text:
        return True
    # 正常攻略标题最多带一个「阴阳师」，出现三次以上基本就是关键词堆砌
    return text.count("阴阳师") >= 3


def filter_items(items: list[GuideItem]) -> tuple[list[GuideItem], int]:
    """返回 (保留的条目, 丢弃数量)。"""
    kept: list[GuideItem] = []
    dropped = 0
    for item in items:
        if is_spam(item.title, item.digest) or is_seo_junk(item.title):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped
