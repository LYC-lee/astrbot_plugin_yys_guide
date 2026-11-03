"""各平台抓取器。统一产出 GuideItem，失败返回空列表而不是抛异常。"""

from yys_fetchers.base import BaseFetcher
from yys_fetchers.bilibili import BilibiliFetcher
from yys_fetchers.douyin import DouyinFetcher
from yys_fetchers.ds163 import Ds163Fetcher
from yys_fetchers.nga import NgaFetcher
from yys_fetchers.weibo import WeiboFetcher
from yys_fetchers.websearch import WebSearchFetcher

__all__ = [
    "BaseFetcher",
    "BilibiliFetcher",
    "DouyinFetcher",
    "Ds163Fetcher",
    "NgaFetcher",
    "WeiboFetcher",
    "WebSearchFetcher",
]
