from platforms.browser import BrowserPlatform


class TaobaoBot(BrowserPlatform):
    """淘宝/千牛网页会话适配器；需在真实商家页面配置并验收选择器。"""
    platform_name = 'taobao'
