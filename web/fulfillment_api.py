"""Routes inherit the console's loopback, origin and write-header protections."""
from fastapi import APIRouter
from core.fulfillment_runtime import FulfillmentRuntime
from core.fulfillment_store import FulfillmentError


def fulfillment_router(runtime: FulfillmentRuntime):
    router = APIRouter(prefix="/api/fulfillment")

    def shape(data, allowed, required=()):
        if set(data) - set(allowed) or not set(required) <= set(data):
            raise FulfillmentError("invalid_request", "发货请求格式无效；付款状态只能从已授权订单接口核验")
        return data

    @router.get("")
    async def state():
        return runtime.state()

    @router.get("/jobs")
    async def jobs(status: str = "all", offset: int = 0, limit: int = 50):
        rows = runtime.store.list_jobs(limit=limit, offset=offset, status=status)
        counts = runtime.store.job_counts()
        return {"jobs": rows, "counts": counts, "offset": offset, "limit": limit,
                "total": counts[status], "has_more": offset + len(rows) < counts[status]}

    @router.put("/rules")
    async def rule(data: dict):
        return {"rule": runtime.store.save_rule(data)}

    @router.post("/cards")
    async def cards(data: dict):
        shape(data, ("rule_id", "cards"), ("rule_id", "cards"))
        return runtime.store.import_cards(data["rule_id"], data["cards"])

    @router.put("/sources")
    async def source(data: dict):
        return {"source": runtime.save_source(data)}

    @router.post("/sources/{source_id}/test")
    async def test(source_id: str):
        return await runtime.test(source_id)

    @router.post("/sync")
    async def sync(data: dict):
        shape(data, ("source_id", "dry_run"), ("source_id",))
        return await runtime.sync(data["source_id"], dry_run=data.get("dry_run", True))

    @router.post("/process")
    async def process(data: dict):
        shape(data, ("source_id", "order_id", "dry_run"), ("source_id", "order_id"))
        return await runtime.process(data["source_id"], data["order_id"], dry_run=data.get("dry_run", True))

    @router.post("/jobs/{job_id}/tracking")
    async def tracking(job_id: str, data: dict):
        shape(data, ("carrier", "tracking_number"), ("carrier", "tracking_number"))
        return {"job": runtime.store.record_tracking(job_id, data["carrier"], data["tracking_number"])}

    @router.post("/jobs/{job_id}/retry")
    async def retry(job_id: str, data: dict):
        shape(data, ("dry_run",))
        return await runtime.retry(job_id, dry_run=data.get("dry_run", True))

    @router.post("/start")
    async def start():
        return await runtime.start()

    @router.post("/stop")
    async def stop():
        return await runtime.stop()

    return router
