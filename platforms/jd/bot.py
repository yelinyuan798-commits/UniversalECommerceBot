from platforms.browser import BrowserPlatform


class JingdongBot(BrowserPlatform):
    """京东/咚咚网页会话适配器；需在真实商家页面配置并验收选择器。"""
    platform_name = 'jingdong'
