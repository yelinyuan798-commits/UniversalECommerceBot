"""Bounded HTTP access for the existing, unofficial Xianyu web protocol."""
import json
import os
import time
from pathlib import Path

import requests
from dotenv import set_key

from utils.helpers import generate_sign


class AuthenticationRequired(RuntimeError):
    pass


class PlatformRequestError(RuntimeError):
    pass


class XianyuApis:
    def __init__(self, env_path=None, cookie_env='COOKIES_STR'):
        self.cookie_env = cookie_env
        self.env_path = Path(env_path or Path(__file__).resolve().parents[2] / 'config' / '.env')
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json', 'origin': 'https://www.goofish.com',
            'referer': 'https://www.goofish.com/',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/133.0.0.0 Safari/537.36',
        })

    def cookie_string(self):
        return '; '.join(f'{cookie.name}={cookie.value}' for cookie in self.session.cookies)

    def clear_duplicate_cookies(self):
        latest = {}
        for cookie in self.session.cookies:
            latest[cookie.name] = cookie
        jar = requests.cookies.RequestsCookieJar()
        for cookie in latest.values():
            jar.set_cookie(cookie)
        self.session.cookies = jar

    def update_env_cookies(self):
        if not self.env_path.is_file():
            return
        self.clear_duplicate_cookies()
        set_key(str(self.env_path), self.cookie_env, self.cookie_string())
        if os.name != 'nt':
            self.env_path.chmod(0o600)

    def hasLogin(self, retry_count=0):
        try:
            response = self.session.post('https://passport.goofish.com/newlogin/hasLogin.do',
                params={'appName': 'xianyu', 'fromSite': '77'},
                data={'hid': self.session.cookies.get('unb', ''), 'ltl': 'true',
                      'appName': 'xianyu', 'fromSite': '77',
                      '_csrf_token': self.session.cookies.get('XSRF-TOKEN', '')}, timeout=(5, 20))
            response.raise_for_status()
            ok = response.json().get('content', {}).get('success') is True
            if ok:
                self.update_env_cookies()
            return ok
        except (requests.RequestException, ValueError, AttributeError):
            return False

    def _call_mtop(self, api, data):
        for attempt in range(2):
            self.clear_duplicate_cookies()
            timestamp = str(int(time.time() * 1000))
            token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
            if not token:
                raise AuthenticationRequired('闲鱼登录信息不完整，请在本机重新登录')
            payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
            params = dict(jsv='2.7.2', appKey='34839810', t=timestamp, v='1.0',
                          type='originaljson', accountSite='xianyu', dataType='json',
                          timeout='20000', api=api, sessionOption='AutoLoginOnly',
                          sign=generate_sign(timestamp, token, payload))
            try:
                response = self.session.post(f'https://h5api.m.goofish.com/h5/{api}/1.0/',
                                             params=params, data={'data': payload}, timeout=(5, 20))
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError('invalid response')
                codes = result.get('ret', [])
                if any(isinstance(code, str) and code.startswith('SUCCESS') for code in codes):
                    self.update_env_cookies()
                    return result
                code_text = ' '.join(str(code) for code in codes)
                if any(word in code_text for word in ('SESSION_EXPIRED', 'ILLEGAL_ACCESS', 'RGV587', 'USER_VALIDATE')):
                    raise AuthenticationRequired('闲鱼登录已失效或需要验证，请在本机打开闲鱼完成验证')
                if any(word in code_text for word in ('TOKEN_EXOIRED', 'TOKEN_EXPIRED')):
                    # A token-expired response may rotate the H5 signing cookie.
                    # Re-sign at most once, only when the server supplied a new token.
                    self.clear_duplicate_cookies()
                    rotated = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
                    if attempt == 0 and rotated and rotated != token:
                        self.update_env_cookies()
                        continue
                    raise AuthenticationRequired('闲鱼签名凭据已失效，请在本机重新登录')
            except AuthenticationRequired:
                raise
            except (requests.RequestException, ValueError, TypeError):
                pass
            if attempt == 0:
                time.sleep(0.5)
        raise PlatformRequestError('闲鱼接口暂时不可用；未输出含登录信息的原始响应')

    def get_token(self, device_id, retry_count=0):
        return self._call_mtop('mtop.taobao.idlemessage.pc.login.token',
                               {'appKey': '444e9908a51d1cb236a27862abc769c9', 'deviceId': device_id})

    def get_item_info(self, item_id, retry_count=0):
        return self._call_mtop('mtop.taobao.idle.pc.detail', {'itemId': str(item_id)})
