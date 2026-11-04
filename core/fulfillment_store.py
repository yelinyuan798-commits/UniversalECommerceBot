"""Private inventory and durable, transactionally allocated fulfillment jobs."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import unicodedata
import uuid


class FulfillmentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def text_field(value, *, optional=False, maximum=200):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(unicodedata.category(c).startswith("C") for c in value):
        raise FulfillmentError("invalid_field", "发货字段格式无效，请检查必填内容和长度")
    return value.strip()


def delivery_content_field(value) -> str:
    """Resource instructions may span lines; identifiers and cards may not."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 10000
            or any(unicodedata.category(char).startswith("C") and char not in "\n\r\t" for char in value)):
        raise FulfillmentError("invalid_delivery_content", "资料正文须为 1 至 10000 字，可包含换行和制表，不可包含其他控制字符")
    return value


def money(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise FulfillmentError("invalid_amount", "金额必须为非负数字，最多保留两位小数")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999") or amount != amount.quantize(Decimal("0.01")):
            raise InvalidOperation()
        return format(amount, ".2f")
    except (InvalidOperation, ValueError, OverflowError):
        raise FulfillmentError("invalid_amount", "金额必须为非负数字，最多保留两位小数") from None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _identity(*parts) -> str:
    return hashlib.sha256(_json(parts).encode()).hexdigest()


_ACTIVE_CLAIMS: set[str] = set()
_CLAIM_LOCK = threading.RLock()
_PUBLIC_JOB = ("id", "platform", "shop_id", "order_id", "line_id", "buyer_id", "item_id", "sku_id", "rule_id", "type", "quantity", "amount", "currency", "status", "error_code", "attempts", "carrier", "tracking_number", "created_at", "updated_at")


class FulfillmentStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        try:
            if self.path != ":memory:":
                location = Path(self.path)
                location.parent.mkdir(parents=True, exist_ok=True)
                if location.is_symlink():
                    raise OSError()
                fd = os.open(location, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
                os.chmod(location, 0o600)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS fulfillment_rules (
                    id TEXT PRIMARY KEY, platform TEXT NOT NULL, shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL, sku_id TEXT NOT NULL DEFAULT '', type TEXT NOT NULL,
                    enabled INTEGER NOT NULL, min_unit_amount TEXT NOT NULL, currency TEXT NOT NULL,
                    delivery_content TEXT NOT NULL DEFAULT '',
                    UNIQUE(platform,shop_id,item_id,sku_id)
                );
                CREATE TABLE IF NOT EXISTS fulfillment_jobs (
                    id TEXT PRIMARY KEY, platform TEXT NOT NULL, shop_id TEXT NOT NULL,
                    order_id TEXT NOT NULL, line_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
                    item_id TEXT NOT NULL, sku_id TEXT NOT NULL, rule_id TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL, quantity INTEGER NOT NULL, amount TEXT NOT NULL,
                    currency TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                    reference_json TEXT NOT NULL, order_json TEXT NOT NULL, rule_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER, owner_token TEXT,
                    carrier TEXT NOT NULL DEFAULT '', tracking_number TEXT NOT NULL DEFAULT '',
                    receipt_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(platform,shop_id,order_id,line_id)
                );
                CREATE TABLE IF NOT EXISTS fulfillment_cards (
                    id INTEGER PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES fulfillment_rules(id),
                    fingerprint TEXT NOT NULL UNIQUE, secret TEXT NOT NULL,
                    assigned_job TEXT REFERENCES fulfillment_jobs(id),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS fulfillment_stock ON fulfillment_cards(rule_id,assigned_job);
                CREATE INDEX IF NOT EXISTS fulfillment_job_status ON fulfillment_jobs(status);
            """)
            self.recover_interrupted()
        except (OSError, sqlite3.Error):
            raise FulfillmentError("storage_error", "发货数据无法打开，请检查本地文件权限") from None

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise FulfillmentError("storage_error", "发货数据保存失败，请稍后重试") from None
            except BaseException:
                self._conn.rollback()
                raise

    def recover_interrupted(self):
        """Never recover a live send owned by another store or process."""
        with _CLAIM_LOCK, self._transaction() as conn:
            rows = conn.execute("SELECT id,owner_pid,owner_token FROM fulfillment_jobs WHERE status='sending'").fetchall()
            for row in rows:
                if row["owner_pid"] == os.getpid():
                    alive = row["owner_token"] in _ACTIVE_CLAIMS
                elif not row["owner_pid"]:
                    alive = False
                else:
                    try:
                        os.kill(row["owner_pid"] or -1, 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                    except (OSError, TypeError):
                        alive = True  # Unable to establish termination: do not steal it.
                if not alive:
                    conn.execute("UPDATE fulfillment_jobs SET status='unknown',error_code='interrupted',owner_token=NULL,owner_pid=NULL WHERE id=?", (row["id"],))

    def save_rule(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise FulfillmentError("invalid_rule", "发货规则必须是对象")
        allowed = {"id", "platform", "shop_id", "item_id", "sku_id", "type", "enabled", "min_unit_amount", "currency", "delivery_content"}
        if set(data) - allowed:
            raise FulfillmentError("invalid_rule", "发货规则包含不支持的字段")
        key = [text_field(data.get(name), optional=name == "sku_id") for name in ("platform", "shop_id", "item_id", "sku_id")]
        rule_id = _identity(*key)
        if data.get("id") and data["id"] != rule_id:
            raise FulfillmentError("rule_identity_changed", "修改平台、店铺或商品后请另建发货规则")
        kind = data.get("type")
        if not isinstance(kind, str) or kind not in {"card", "link", "physical"} or not isinstance(data.get("enabled", True), bool):
            raise FulfillmentError("invalid_rule", "请选择卡密、资料链接或实物，并填写启用状态")
        price = money(data.get("min_unit_amount"))
        currency = text_field(data.get("currency", "CNY"), maximum=3).upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise FulfillmentError("invalid_currency", "币种须为 CNY 等三位英文代码")
        with self._transaction() as conn:
            previous = conn.execute("SELECT * FROM fulfillment_rules WHERE id=?", (rule_id,)).fetchone()
            content = data.get("delivery_content")
            if kind == "link":
                if content in (None, "") and previous and previous["type"] == "link":
                    content = previous["delivery_content"]
                content = delivery_content_field(content)
            else:
                content = ""
            conn.execute("""INSERT INTO fulfillment_rules VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET type=excluded.type,enabled=excluded.enabled,
                min_unit_amount=excluded.min_unit_amount,currency=excluded.currency,delivery_content=excluded.delivery_content""",
                (rule_id, *key, kind, int(data.get("enabled", True)), price, currency, content))
        return next(rule for rule in self.list_rules() if rule["id"] == rule_id)

    def list_rules(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("""SELECT r.*,COUNT(c.id) AS inventory_total,
                COUNT(CASE WHEN c.assigned_job IS NULL THEN c.id END) AS inventory_available
                FROM fulfillment_rules r LEFT JOIN fulfillment_cards c ON c.rule_id=r.id GROUP BY r.id ORDER BY r.rowid""").fetchall()
        result = []
        for row in rows:
            rule = dict(row)
            rule["has_delivery_content"] = bool(rule.pop("delivery_content"))
            rule["enabled"] = bool(rule["enabled"])
            result.append(rule)
        return result

    def import_cards(self, rule_id: str, cards: list[str]) -> dict:
        rule_id = text_field(rule_id)
        if not isinstance(cards, list) or not cards or len(cards) > 10000:
            raise FulfillmentError("invalid_cards", "请导入 1 至 10000 条卡密")
        secrets = [text_field(value, maximum=10000) for value in cards]
        added = 0
        with self._transaction() as conn:
            rule = conn.execute("SELECT type FROM fulfillment_rules WHERE id=?", (rule_id,)).fetchone()
            if not rule or rule["type"] != "card":
                raise FulfillmentError("invalid_rule", "请选择已有的卡密发货规则")
            for secret in secrets:
                added += conn.execute("INSERT OR IGNORE INTO fulfillment_cards(rule_id,fingerprint,secret) VALUES (?,?,?)", (rule_id, hashlib.sha256(secret.encode()).hexdigest(), secret)).rowcount
            available = conn.execute("SELECT COUNT(*) FROM fulfillment_cards WHERE rule_id=? AND assigned_job IS NULL", (rule_id,)).fetchone()[0]
        return {"added": added, "duplicates": len(cards) - added, "available": available}

    @staticmethod
    def _rule_for(conn, order, item):
        row = conn.execute("""SELECT * FROM fulfillment_rules WHERE platform=? AND shop_id=? AND item_id=?
            AND sku_id IN (?, '') ORDER BY (sku_id=?) DESC LIMIT 1""",
            (order["platform"], order["shop_id"], item["item_id"], item["sku_id"], item["sku_id"])).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _rule_error(rule, order, item):
        if not rule:
            return "rule_missing"
        if not rule["enabled"]:
            return "rule_disabled"
        if rule["currency"] != order["currency"] or Decimal(rule["min_unit_amount"]) * item["quantity"] > Decimal(item["amount"]):
            return "price_mismatch"
        return ""

    def preview(self, order: dict) -> list[dict]:
        result = []
        with self._lock:
            remaining = {}
            for item in order["items"]:
                existing = self.get_job(_identity(order["platform"], order["shop_id"], order["order_id"], item["line_id"]))
                if existing:
                    result.append({**existing, "existing": True})
                    continue
                rule = self._rule_for(self._conn, order, item)
                error = self._rule_error(rule, order, item)
                if rule and rule["type"] == "card" and not error:
                    if rule["id"] not in remaining:
                        remaining[rule["id"]] = self._conn.execute("SELECT COUNT(*) FROM fulfillment_cards WHERE rule_id=? AND assigned_job IS NULL", (rule["id"],)).fetchone()[0]
                    if remaining[rule["id"]] < item["quantity"]:
                        error = "insufficient_inventory"
                    else:
                        remaining[rule["id"]] -= item["quantity"]
                result.append({"line_id": item["line_id"], "item_id": item["item_id"], "sku_id": item["sku_id"], "type": rule["type"] if rule else "", "quantity": item["quantity"], "status": "blocked" if error else "awaiting_shipment" if rule["type"] == "physical" else "ready", "error_code": error})
        return result

    def reserve_order(self, reference: dict, order: dict) -> list[dict]:
        ids = []
        with self._transaction() as conn:
            for item in order["items"]:
                job_id = _identity(order["platform"], order["shop_id"], order["order_id"], item["line_id"])
                ids.append(job_id)
                if conn.execute("SELECT id FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone():
                    continue
                rule = self._rule_for(conn, order, item)
                error = self._rule_error(rule, order, item)
                kind = rule["type"] if rule else ""
                status = "blocked" if error else "awaiting_shipment" if kind == "physical" else "reserved"
                conn.execute("""INSERT INTO fulfillment_jobs
                    (id,platform,shop_id,order_id,line_id,buyer_id,item_id,sku_id,rule_id,type,quantity,amount,currency,status,error_code,reference_json,order_json,rule_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (job_id, order["platform"], order["shop_id"], order["order_id"], item["line_id"], order["buyer_id"], item["item_id"], item["sku_id"], rule["id"] if rule else "", kind, item["quantity"], item["amount"], order["currency"], status, error, _json(reference), _json(order), _json(rule or {})))
                if status == "reserved" and kind == "card":
                    self._allocate(conn, job_id, rule["id"], item["quantity"])
        return [self.get_job(job_id) for job_id in ids]

    @staticmethod
    def _allocate(conn, job_id, rule_id, quantity) -> bool:
        assigned = conn.execute("SELECT COUNT(*) FROM fulfillment_cards WHERE assigned_job=?", (job_id,)).fetchone()[0]
        if assigned == quantity:
            return True
        if assigned:
            raise FulfillmentError("inventory_inconsistent", "卡密分配记录异常，请人工核对")
        available = conn.execute("SELECT id FROM fulfillment_cards WHERE rule_id=? AND assigned_job IS NULL ORDER BY id LIMIT ?", (rule_id, quantity)).fetchall()
        if len(available) != quantity:
            conn.execute("UPDATE fulfillment_jobs SET status='blocked',error_code='insufficient_inventory' WHERE id=?", (job_id,))
            return False
        conn.executemany("UPDATE fulfillment_cards SET assigned_job=? WHERE id=? AND assigned_job IS NULL", [(job_id, row[0]) for row in available])
        return True

    def private_job(self, job_id: str) -> dict | None:
        """For the engine only. Never expose this through HTTP or event logs."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("reference", "order", "rule"):
                result[key] = json.loads(result.pop(key + "_json"))
            result["cards"] = [row[0] for row in self._conn.execute("SELECT secret FROM fulfillment_cards WHERE assigned_job=? ORDER BY id", (job_id,))]
            return result

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = {key: row[key] for key in _PUBLIC_JOB}
            result["source_id"] = json.loads(row["reference_json"]).get("source_id", "")
            result["assigned_count"] = self._conn.execute("SELECT COUNT(*) FROM fulfillment_cards WHERE assigned_job=?", (job_id,)).fetchone()[0]
            return result

    def list_jobs(self, limit=100, offset=0, status="all") -> list[dict]:
        groups = {"all": (), "pending": ("awaiting_shipment", "reserved", "sending"),
                  "unknown": ("unknown",), "sent": ("sent",), "blocked": ("blocked",)}
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 9223372036854775807
                or not isinstance(status, str) or status not in groups):
            raise FulfillmentError("invalid_pagination", "请选择有效的任务状态、每页 1 至 100 条和非负分页位置")
        states = groups[status]
        where = " WHERE status IN (" + ",".join("?" for _ in states) + ")" if states else ""
        with self._lock:
            rows = self._conn.execute("SELECT id FROM fulfillment_jobs" + where + " ORDER BY rowid DESC LIMIT ? OFFSET ?", (*states, limit, offset)).fetchall()
            return [self.get_job(row[0]) for row in rows]

    def job_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT status,COUNT(*) FROM fulfillment_jobs GROUP BY status").fetchall()
        counts = dict(rows)
        return {"all": sum(counts.values()),
                "pending": sum(counts.get(status, 0) for status in ("awaiting_shipment", "reserved", "sending")),
                **{status: counts.get(status, 0) for status in ("unknown", "sent", "blocked")}}

    def jobs_for_order(self, reference: dict) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM fulfillment_jobs WHERE platform=? AND shop_id=? AND order_id=? ORDER BY line_id", tuple(reference[key] for key in ("platform", "shop_id", "order_id"))).fetchall()
            return [self.get_job(row[0]) for row in rows]

    def block(self, job_id: str, error_code: str):
        allowed = {"verification_failed", "order_unpaid", "order_cancelled", "order_refunded", "order_fulfilled", "order_changed", "rule_missing", "rule_disabled", "rule_changed", "price_mismatch", "tracking_required"}
        if error_code not in allowed:
            error_code = "verification_failed"
        with self._transaction() as conn:
            conn.execute("UPDATE fulfillment_jobs SET status='blocked',error_code=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status IN ('reserved','blocked','awaiting_shipment')", (error_code, job_id))

    def current_rule_error(self, job: dict) -> str:
        """An old snapshot cannot bypass a merchant's current pause or price floor."""
        item = next(item for item in job["order"]["items"] if item["line_id"] == job["line_id"])
        with self._lock:
            current = self._rule_for(self._conn, job["order"], item)
        error = self._rule_error(current, job["order"], item)
        if error:
            return error
        if current["id"] != job["rule_id"] or current["type"] != job["type"]:
            return "rule_changed"
        return ""

    def bind_missing_rule(self, job_id: str):
        """Explicit retry may repair a never-attempted job originally missing a rule."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "blocked" or row["rule_id"] or row["attempts"]:
                return
            if conn.execute("SELECT 1 FROM fulfillment_cards WHERE assigned_job=?", (job_id,)).fetchone():
                return
            order = json.loads(row["order_json"])
            item = next(item for item in order["items"] if item["line_id"] == row["line_id"])
            rule = self._rule_for(conn, order, item)
            if rule:
                conn.execute("UPDATE fulfillment_jobs SET rule_id=?,type=?,rule_json=? WHERE id=?", (rule["id"], rule["type"], _json(rule), job_id))

    def prepare_retry(self, job_id: str) -> bool:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"blocked", "awaiting_shipment"}:
                return False
            rule = json.loads(row["rule_json"])
            if not rule:
                return False
            if row["type"] == "physical" and not (row["carrier"] and row["tracking_number"]):
                conn.execute("UPDATE fulfillment_jobs SET status='awaiting_shipment',error_code='' WHERE id=?", (job_id,))
                return False
            if row["type"] == "card" and not self._allocate(conn, job_id, row["rule_id"], row["quantity"]):
                return False
            conn.execute("UPDATE fulfillment_jobs SET status='reserved',error_code='' WHERE id=?", (job_id,))
            return True

    def record_tracking(self, job_id: str, carrier: str, tracking_number: str) -> dict:
        carrier = text_field(carrier, maximum=100)
        tracking_number = text_field(tracking_number, maximum=100)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM fulfillment_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["type"] != "physical" or row["status"] not in {"awaiting_shipment", "blocked"}:
                raise FulfillmentError("tracking_locked", "该任务不能修改物流信息，请先核对发货状态")
            conn.execute("UPDATE fulfillment_jobs SET carrier=?,tracking_number=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (carrier, tracking_number, job_id))
        return self.get_job(job_id)

    def claim_sending(self, job_id: str) -> str | None:
        token = uuid.uuid4().hex
        with _CLAIM_LOCK, self._transaction() as conn:
            job = self.private_job(job_id)
            if not job or job["status"] != "reserved":
                return None
            error = self.current_rule_error(job)
            if error:
                conn.execute("UPDATE fulfillment_jobs SET status='blocked',error_code=? WHERE id=?", (error, job_id))
                return None
            cursor = conn.execute("UPDATE fulfillment_jobs SET status='sending',attempts=attempts+1,owner_pid=?,owner_token=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status='reserved'", (os.getpid(), token, job_id))
            if cursor.rowcount != 1:
                return None
            _ACTIVE_CLAIMS.add(token)
        return token

    def finish(self, job_id: str, token: str, status: str, *, receipt=None):
        state = status if status in {"sent", "unknown", "failed"} else "unknown"
        error = "" if state == "sent" else "delivery_failed" if state == "failed" else "delivery_uncertain"
        digest = hashlib.sha256(receipt.encode()).hexdigest() if isinstance(receipt, str) else ""
        with _CLAIM_LOCK:
            try:
                with self._transaction() as conn:
                    conn.execute("UPDATE fulfillment_jobs SET status=?,error_code=?,receipt_hash=?,owner_pid=NULL,owner_token=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status='sending' AND owner_token=?", ("blocked" if state == "failed" else state, error, digest, job_id, token))
            finally:
                _ACTIVE_CLAIMS.discard(token)

    def close(self):
        with self._lock:
            self._conn.close()
