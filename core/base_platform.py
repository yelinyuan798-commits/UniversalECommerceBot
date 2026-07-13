from abc import ABC, abstractmethod
from typing import Callable, Optional

class BasePlatform(ABC):
    """所有电商平台的基类"""
    
    def __init__(self, config: dict):
        self.config = config
        self.name = config.get("name", "unknown")
        self.enabled = config.get("enabled", True)
        self._running = False
    
    @abstractmethod
    async def connect(self):
        """连接到平台服务器"""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """断开连接"""
        pass
    
    @abstractmethod
    async def start(self, message_handler: Callable):
        """开始监听消息
        message_handler(user_id, item_id, message, platform) -> reply
        """
        pass
    
    @abstractmethod
    async def send_message(self, user_id: str, message: str):
        """发送消息给用户"""
        pass
    
    @abstractmethod
    async def send_delivery(self, user_id: str, item_id: str, delivery_info: dict):
        """发送发货信息"""
        pass
    
    @abstractmethod
    async def get_item_info(self, item_id: str) -> dict:
        """获取商品信息"""
        pass
    
    @property
    def is_running(self) -> bool:
        return self._running
