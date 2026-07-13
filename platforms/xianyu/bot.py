"""闲鱼平台机器人 - 实现 BasePlatform 接口"""
import os, sys, json, base64, time, ssl, re, random, asyncio
import websockets
import certifi
from dotenv import load_dotenv
from loguru import logger
from core.base_platform import BasePlatform
from platforms.xianyu.api import XianyuApis
from utils.helpers import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from core.context import ChatContextManager

load_dotenv("config/.env")

class XianyuBot(BasePlatform):
    """闲鱼平台机器人"""
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.api = XianyuApis()
        self.cookies_str = None
        self.myid = None
        self.device_id = None
        self.ws = None
        self.context_manager = ChatContextManager(db_path="data/xianyu_chat.db")
        self.delivery_config_path = "config/delivery_items.json"
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))
        self.current_token = None
        self._message_handler = None
    
    def load_platform_config(self):
        """从 .env 和环境变量加载配置"""
        logger.info("正在初始化闲鱼平台...")
        self.cookies_str = os.getenv("COOKIES_STR", "")
        if not self.cookies_str:
            logger.error("COOKIES_STR 未配置")
            return False
        cookies = trans_cookies(self.cookies_str)
        self.api.session.cookies.update(cookies)
        self.myid = cookies.get("unb", "")
        if not self.myid:
            logger.error("Cookie 缺少 unb 字段")
            return False
        self.device_id = generate_device_id(self.myid)
        return True
    
    async def connect(self):
        logger.info("正在连接闲鱼服务器...")
        token_result = self.api.get_token(self.device_id)
        if "data" in token_result and "accessToken" in token_result["data"]:
            self.current_token = token_result["data"]["accessToken"]
            logger.info("Token 获取成功")
            return True
        logger.error("Token 获取失败")
        return False
    
    async def disconnect(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
        logger.info("闲鱼连接已断开")
    
    async def send_message(self, user_id: str, message: str):
        if not self.ws or not self.myid:
            return False
        try:
            text = {"contentType": 1, "text": {"text": message}}
            text_base64 = str(base64.b64encode(json.dumps(text).encode("utf-8")), "utf-8")
            msg = {
                "lwp": "/r/MessageSend/sendByReceiverScope",
                "headers": {"mid": generate_mid()},
                "body": [{
                    "uuid": generate_uuid(),
                    "cid": f"{user_id}@goofish",
                    "conversationType": 1,
                    "content": {"contentType": 101, "custom": {"type": 1, "data": text_base64}},
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1
                }, {"actualReceivers": [f"{user_id}@goofish", f"{self.myid}@goofish"]}]
            }
            await self.ws.send(json.dumps(msg))
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False
    
    async def send_delivery(self, user_id: str, item_id: str, delivery_info: dict):
        """自动发货"""
        if not delivery_info or not delivery_info.get("link"):
            logger.warning(f"商品 {item_id} 缺少发货链接")
            return
        name = delivery_info.get("name", "资料")
        link = delivery_info["link"]
        code = delivery_info.get("code", "")
        note = delivery_info.get("note", "")
        parts = [f"您好，{name}已准备好，百度网盘发货：", f"领取链接：{link}"]
        if code:
            parts.append(f"提取码：{code}")
        if note:
            parts.append(note)
        delivery_msg = "\n".join(parts)
        await self.send_message(user_id, delivery_msg)
        logger.info(f"已自动发货给 {user_id}，商品 {item_id}")
    
    async def get_item_info(self, item_id: str) -> dict:
        result = self.api.get_item_info(item_id)
        if "data" in result and "itemDO" in result["data"]:
            return result["data"]["itemDO"]
        return {"title": f"商品 {item_id}", "desc": "", "soldPrice": 0, "quantity": 1}
    
    def load_delivery_config(self):
        try:
            if not os.path.exists(self.delivery_config_path):
                return {}
            with open(self.delivery_config_path, encoding="utf-8") as f:
                config = json.load(f)
            return config.get("items", config)
        except:
            return {}
    
    def build_delivery_message(self, item_id):
        items = self.load_delivery_config()
        item = items.get(str(item_id))
        if not item:
            return None
        if isinstance(item, str):
            return item.strip()
        if not item.get("enabled", True):
            return None
        name = item.get("name", "资料")
        link = item.get("link", "").strip()
        code = item.get("code", "").strip()
        note = item.get("note", "").strip()
        msg = item.get("message", "").strip()
        if msg:
            return msg
        if not link:
            return None
        parts = [f"您好，{name}已准备好，百度网盘发货："]
        parts.append(f"领取链接：{link}")
        if code:
            parts.append(f"提取码：{code}")
        if note:
            parts.append(note)
        return "\n".join(parts)
    
    def is_chat_message(self, msg):
        try:
            return (isinstance(msg, dict) and "1" in msg
                    and isinstance(msg["1"], dict) and "10" in msg["1"]
                    and isinstance(msg["1"]["10"], dict)
                    and "reminderContent" in msg["1"]["10"])
        except:
            return False
    
    def extract_info(self, msg):
        try:
            create_time = int(msg["1"]["5"])
            send_user_id = msg["1"]["10"]["senderUserId"]
            send_message = msg["1"]["10"]["reminderContent"]
            url_info = msg["1"]["10"].get("reminderUrl", "")
            item_id = ""
            if "itemId=" in url_info:
                item_id = url_info.split("itemId=")[1].split("&")[0]
            chat_id = msg["1"]["2"].split("@")[0] if "@" in msg["1"]["2"] else msg["1"]["2"]
            return create_time, send_user_id, send_message, item_id, chat_id
        except:
            return None, None, None, None, None
    
    def is_paid_order(self, msg):
        try:
            return msg.get("3", {}).get("redReminder") == "等待卖家发货"
        except:
            return False
    
    def is_paid_delivery_notice(self, msg_text):
        return isinstance(msg_text, str) and "已付款" in msg_text and "等待" in msg_text and "发货" in msg_text
    
    async def start(self, message_handler):
        """启动闲鱼平台 - 主循环"""
        if not self.load_platform_config():
            return
        self._message_handler = message_handler
        reconnect_wait = 5
        
        while True:
            try:
                if not await self.connect():
                    logger.error("连接失败，等待重试...")
                    await asyncio.sleep(reconnect_wait)
                    continue
                
                headers = {"Cookie": self.cookies_str, "Host": "wss-goofish.dingtalk.com",
                           "Connection": "Upgrade", "User-Agent": "Mozilla/5.0 ... Chrome/133.0.0.0 Safari/537.36"}
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                
                async with websockets.connect("wss://wss-goofish.dingtalk.com/",
                                              extra_headers=headers, ssl=ssl_ctx) as ws:
                    self.ws = ws
                    await self._register(ws)
                    reconnect_wait = 5
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            if "code" in data and data["code"] == 200:
                                continue
                            if "body" in data and "syncPushPackage" in data.get("body", {}):
                                await self._process_sync(data, ws)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"消息处理异常: {e}")
                            
            except websockets.exceptions.ConnectionClosed:
                logger.warning("连接断开，重连中...")
            except Exception as e:
                logger.error(f"连接异常: {e}")
            
            self.ws = None
            await asyncio.sleep(reconnect_wait)
            reconnect_wait = min(reconnect_wait * 1.5, 60)
    
    async def _register(self, ws):
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ... DingWeb/2.1.5",
                "dt": "j", "wv": "im:3,au:3,sy:6",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        await asyncio.sleep(1)
        sync = {
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [{"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync",
                      "topic": "sync", "highPts": 0,
                      "pts": int(time.time() * 1000) * 1000,
                      "seq": 0, "timestamp": int(time.time() * 1000)}]
        }
        await ws.send(json.dumps(sync))
        logger.info("✅ 闲鱼连接注册完成")
    
    async def _process_sync(self, data, ws):
        try:
            sync_data = data["body"]["syncPushPackage"]["data"][0]
            if "data" not in sync_data:
                return
            raw = sync_data["data"]
            try:
                decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
                return
            except:
                decrypted = decrypt(raw)
                message = json.loads(decrypted)
            
            # 处理订单消息
            if isinstance(message, dict) and "3" in message:
                reminder = message["3"].get("redReminder", "")
                if reminder == "等待卖家发货":
                    await self._handle_order(message, ws)
                    return
                elif reminder in ("等待买家付款", "交易关闭"):
                    return
            
            # 处理聊天消息
            if not self.is_chat_message(message):
                return
            
            _, user_id, msg_text, item_id, chat_id = self.extract_info(message)
            if not all([user_id, msg_text, item_id]):
                return
            
            # 过滤自己的消息和系统消息
            if user_id == self.myid:
                return
            
            # 付款通知处理
            if self.is_paid_delivery_notice(msg_text):
                delivery = self.build_delivery_message(item_id)
                if delivery:
                    await self.send_message(chat_id or user_id, delivery)
                return
            
            logger.info(f"[闲鱼] {user_id}: {msg_text}")
            
            # 调用统一消息处理器
            if self._message_handler:
                reply = await self._message_handler(user_id, item_id, msg_text, "闲鱼")
                if reply:
                    logger.info(f"[闲鱼回复] {reply}")
                    await self.send_message(chat_id or user_id, reply)
                    
        except Exception as e:
            logger.error(f"处理消息异常: {e}")
    
    async def _handle_order(self, message, ws):
        """处理订单发货"""
        try:
            user_id = message["1"].split("@")[0] if "@" in message["1"] else message["1"]
            item_id = ""
            url_info = message["3"].get("reminderUrl", "")
            if "itemId=" in url_info:
                item_id = url_info.split("itemId=")[1].split("&")[0]
            if not item_id:
                logger.warning(f"订单缺少商品ID: {user_id}")
                return
            delivery = self.build_delivery_message(item_id)
            if delivery:
                chat_id = message["1"].split("@")[0] if "@" in message["1"] else message["1"]
                await self.send_message(chat_id, delivery)
                logger.info(f"已自动发货 {item_id} → {user_id}")
        except Exception as e:
            logger.error(f"自动发货异常: {e}")
