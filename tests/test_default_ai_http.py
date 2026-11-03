"""Exercise the installed OpenAI SDK against an isolated local HTTP model server."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from core.default_skill import reply_default
from core.service import ServiceEngine
from core.store import StateStore


NATURAL_PRICE = "这件商品的价格，您可以先看看详情页；下单时的实际金额以结算页面显示为准。"
PRIVATE_DIAGNOSTIC = "local-model-private-error-do-not-show"


@contextmanager
def local_model(*, status=200, text=NATURAL_PRICE):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append({"path": self.path, "body": body})
            if status == 200:
                response = {
                    "id": "chatcmpl-local-review", "object": "chat.completion", "created": 1,
                    "model": "local-review-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                 "finish_reason": "stop"}],
                }
            else:
                response = {"error": {"message": PRIVATE_DIAGNOSTIC,
                                      "type": "server_error", "code": "local_test_failure"}}
            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    try:
        with patch.dict(os.environ, {
            "API_KEY": "local-http-test-key", "LLM_ENABLED": "true",
            "MODEL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "MODEL_NAME": "local-review-model", "LLM_TIMEOUT_SECONDS": "3",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def event(message_id, text="多少钱"):
    return {"platform": "xianyu", "shop_id": "http-review-store", "user_id": "test-buyer",
            "conversation_id": "same-conversation", "message_id": message_id,
            "item_id": "retained-product", "text": text}


class DefaultAIHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)

    async def test_real_sdk_adopts_natural_reply_without_retained_facts_or_history(self):
        profile = {
            "merchant_name": "retained-merchant-marker", "reply_mode": "custom",
            "custom_instructions": "retained-private-instruction-marker",
            "products": [{"id": "retained-product", "name": "retained-product-marker",
                          "price": 987654, "min_price": 876543}],
        }
        previous = await ServiceEngine(profile, self.store).reply(event("old-custom"))
        self.store.mark_sent(previous["event_key"])
        profile["reply_mode"] = "default"
        with local_model() as requests:
            result = await asyncio.wait_for(ServiceEngine(profile, self.store).reply(event("new-default")), 5)

        self.assertEqual(result["reply"], NATURAL_PRICE)
        self.assertNotEqual(result["reply"], reply_default("多少钱").reply)
        self.assertEqual(result["reply_source"], "ai")
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        body = requests[0]["body"]
        self.assertEqual(body["model"], "local-review-model")
        self.assertEqual([m for m in body["messages"] if m["role"] == "user"],
                         [{"role": "user", "content": "多少钱"}])
        self.assertFalse(any(m["role"] == "assistant" for m in body["messages"]))
        payload = json.dumps(body, ensure_ascii=False)
        for retained in ("retained-merchant-marker", "retained-product-marker",
                         "retained-private-instruction-marker", "987654", "876543"):
            self.assertNotIn(retained, payload)

    async def test_real_http_error_falls_back_without_exposing_error_details(self):
        profile = {"merchant_name": "本地审核", "reply_mode": "default", "products": []}
        with local_model(status=503) as requests:
            result = await asyncio.wait_for(ServiceEngine(profile, self.store).reply(event("http-error")), 5)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(result["reply"], reply_default("多少钱").reply)
        self.assertEqual(result["reply_source"], "builtin")
        self.assertEqual(result["action"], "reply")
        self.assertEqual(result["status"], "generated")
        self.assertNotIn(PRIVATE_DIAGNOSTIC, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(PRIVATE_DIAGNOSTIC, json.dumps(self.store.get_event(result["event_key"]), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
