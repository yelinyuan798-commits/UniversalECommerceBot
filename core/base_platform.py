from abc import ABC, abstractmethod
from typing import Callable


class BasePlatform(ABC):
    """Adapters receive normalized events and report transport delivery separately."""
    platform_name = 'unknown'

    def __init__(self, config: dict):
        self.config = config
        self.options = config.get('config', {})
        self.name = config.get('name', self.platform_name)
        self.shop_id = str(config.get('shop_id', 'default'))
        self.enabled = config.get('enabled', True)
        self.dry_run = config.get('dry_run', True)
        self._running = False
        self.status = 'stopped'
        self.last_error = ''
        self.on_delivery = None

    async def report_delivery(self, result, status, error=''):
        if self.on_delivery and result.get('event_key'):
            await self.on_delivery(result['event_key'], status, error)

    def check_config(self):
        return []

    @abstractmethod
    async def connect(self):
        pass

    @abstractmethod
    async def disconnect(self):
        pass

    @abstractmethod
    async def start(self, message_handler: Callable):
        """handler(event: dict) -> {reply, action, duplicate, event_key}."""
        pass

    @abstractmethod
    async def send_message(self, user_id: str, message: str, **kwargs):
        pass

    async def send_delivery(self, user_id: str, item_id: str, delivery_info: dict):
        raise NotImplementedError('自动发货需要可信订单接口，本项目当前只自动回复客服消息')

    async def get_item_info(self, item_id: str) -> dict:
        return {}

    @property
    def is_running(self) -> bool:
        return self._running
