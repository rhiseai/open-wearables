"""Keep historical imports out of the workers serving current data.

Routing at publish time covers delay(), apply_async() and send_task(), including
linked-account fan-out. The dedicated history consumers share these two queues,
so a burst of SDK batches does not put every provider pull behind the whole burst.
"""

from typing import Any

from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.schemas.sync_status import SyncScope


def _argument(args: tuple[Any, ...] | list[Any], kwargs: dict[str, Any], name: str, position: int) -> Any:
    return kwargs[name] if name in kwargs else (args[position] if len(args) > position else None)


def sdk_import_queue(sync_type: str | None, historical_export: bool | None) -> str:
    """Explicit SDK scope takes precedence over the legacy webhook suppression flag."""
    if sync_type == SyncScope.HISTORICAL or (sync_type != SyncScope.LIVE and historical_export):
        return CeleryQueue.HISTORICAL_SDK
    return CeleryQueue.SDK_SYNC


def route_historical_task(
    name: str,
    args: tuple[Any, ...] | list[Any] | None,
    kwargs: dict[str, Any] | None,
    options: dict[str, Any],
    task: Any = None,
    **_: Any,
) -> dict[str, str] | None:
    """Classify by the existing task arguments without reading the DB, Redis or S3."""
    args, kwargs = args or (), kwargs or {}
    if name == CeleryTask.SYNC_VENDOR_DATA:
        if _argument(args, kwargs, "is_historical", 4):
            return {"queue": CeleryQueue.HISTORICAL_VENDOR}
    elif name == CeleryTask.PROCESS_SDK_SLEEP:
        if _argument(args, kwargs, "historical", 3):
            return {"queue": CeleryQueue.HISTORICAL_SDK}
    elif name == CeleryTask.PROCESS_SDK_UPLOAD:
        sync_type = _argument(args, kwargs, "sync_type", 8)
        # An explicit live batch can arrive while the same device exports history.
        # Older SDKs use the producer's marker/size classification instead.
        return {"queue": sdk_import_queue(sync_type, _argument(args, kwargs, "historical_export", 6))}
    return None
