"""Single catalog for supported adapters, display names, and validation aliases.

Portal URLs are official merchant entry points, not hard-coded inbox endpoints.
`user_verified` records the owner's real-store test report on 2026-11-03; it is
not independent reproduction by this project. New adapters have only passed the
local synthetic-inbox suite and still require real-account DOM calibration.
"""

PLATFORM_CATALOG = {
    'xianyu': {
        'name': '闲鱼', 'class': 'platforms.xianyu.bot.XianyuBot',
        'url': 'https://www.goofish.com/', 'verification': 'user_verified',
    },
    'taobao': {
        'name': '淘宝 / 千牛', 'class': 'platforms.taobao.bot.TaobaoBot',
        'url': 'https://myseller.taobao.com/', 'verification': 'user_verified',
    },
    'jingdong': {
        'name': '京东 / 咚咚', 'class': 'platforms.jd.bot.JingdongBot',
        'url': 'https://dongdong.jd.com/', 'verification': 'user_verified',
    },
    'pinduoduo': {
        'name': '拼多多', 'class': 'platforms.pdd.bot.PddBot',
        'url': 'https://mms.pinduoduo.com/', 'verification': 'local_tested',
    },
    'douyin': {
        'name': '抖音 / 抖店', 'class': 'platforms.douyin.bot.DouyinBot',
        'url': 'https://fxg.jinritemai.com/', 'verification': 'local_tested',
    },
    'kuaishou': {
        'name': '快手小店', 'class': 'platforms.kuaishou.bot.KuaishouBot',
        'url': 'https://login.kwaixiaodian.com/', 'verification': 'local_tested',
    },
    'xiaohongshu': {
        'name': '小红书 / 千帆', 'class': 'platforms.xiaohongshu.bot.XiaohongshuBot',
        'url': 'https://ark.xiaohongshu.com/', 'verification': 'local_tested',
    },
}

PLATFORMS = {key: value['class'] for key, value in PLATFORM_CATALOG.items()}
PLATFORM_LABELS = {key: value['name'] for key, value in PLATFORM_CATALOG.items()}
PLATFORM_ALIASES = {
    **{key: key for key in PLATFORMS},
    '闲鱼': 'xianyu', '咸鱼': 'xianyu',
    '淘宝': 'taobao', '天猫': 'taobao', '淘宝/天猫': 'taobao', '淘宝 / 天猫': 'taobao',
    '千牛': 'taobao', '淘宝/千牛': 'taobao', '淘宝 / 千牛': 'taobao',
    '京东': 'jingdong', 'jd': 'jingdong', '咚咚': 'jingdong',
    '京东/咚咚': 'jingdong', '京东 / 咚咚': 'jingdong',
    '拼多多': 'pinduoduo', 'pdd': 'pinduoduo', '多多': 'pinduoduo',
    '抖音': 'douyin', '抖店': 'douyin', '抖音电商': 'douyin',
    '抖音/抖店': 'douyin', '抖音 / 抖店': 'douyin',
    '快手': 'kuaishou', '快手小店': 'kuaishou', '快手电商': 'kuaishou',
    '小红书': 'xiaohongshu', '千帆': 'xiaohongshu', 'xhs': 'xiaohongshu',
    '小红书/千帆': 'xiaohongshu', '小红书 / 千帆': 'xiaohongshu',
}

_VERIFICATION_LABELS = {
    'user_verified': '用户报告已完成实店收发验证',
    'local_tested': '本地模拟测试通过，待实店验证',
}


def platform_metadata() -> list[dict]:
    """Return fresh public metadata for the console without mutable global state."""
    return [dict(id=key, name=value['name'], url=value['url'],
                 verification=value['verification'],
                 verification_label=_VERIFICATION_LABELS[value['verification']])
            for key, value in PLATFORM_CATALOG.items()]
