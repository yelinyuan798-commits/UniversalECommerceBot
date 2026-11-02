from platforms.browser import BrowserPlatform


class DouyinBot(BrowserPlatform):
    """抖店商家网页适配器；须在真实商家会话页面校准并验证。"""
    platform_name = 'douyin'
