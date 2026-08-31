#!/usr/bin/env python3
"""Restrict Lucie's outgoing webhook endpoints to the product events it consumes.

This runs as a one-off task inside the deployed backend environment. It deliberately
uses the backend's existing database and Svix credentials instead of a human admin
password, so production automation does not depend on mutable dashboard credentials.
"""

import logging
import re

from svix.api import EndpointListOptions
from svix.api.errors.http_error import HttpError

from app.database import SessionLocal
from app.services import developer_service
from app.services.outgoing_webhooks import svix as svix_service

logger = logging.getLogger(__name__)

_DEVELOPER_PAGE_SIZE = 250
_ENDPOINT_PAGE_SIZE = 250
_LUCIE_ENDPOINT_PATTERN = re.compile(r"https://api\.getlucie\.ai/(?:api/v1/)?webhooks/ow/?")
_LUCIE_FILTER_TYPES = ["sleep.created", "sleep.updated", "connection.created"]


def configure_lucie_webhook_filters() -> int:
    """Patch every exact Lucie endpoint and return the number configured."""
    if not svix_service.is_enabled():
        raise RuntimeError("Outgoing webhooks are not enabled")

    configured = 0
    with SessionLocal() as db:
        developer_offset = 0
        while True:
            developers = developer_service.crud.get_all(
                db,
                filters={},
                offset=developer_offset,
                limit=_DEVELOPER_PAGE_SIZE,
                sort_by="id",
            )
            for developer in developers:
                endpoint_iterator: str | None = None
                while True:
                    try:
                        page = svix_service.list_endpoints(
                            str(developer.id),
                            EndpointListOptions(limit=_ENDPOINT_PAGE_SIZE, iterator=endpoint_iterator),
                        )
                    except HttpError as exc:
                        if exc.status_code == 404:
                            break
                        raise

                    for endpoint in page.data:
                        if _LUCIE_ENDPOINT_PATTERN.fullmatch(endpoint.url) is None:
                            continue
                        updated = svix_service.patch_endpoint(
                            str(developer.id),
                            endpoint.id,
                            filter_types=_LUCIE_FILTER_TYPES,
                        )
                        if sorted(updated.filter_types or []) != sorted(_LUCIE_FILTER_TYPES):
                            raise RuntimeError("Svix did not persist the required Lucie event filters")
                        configured += 1

                    if page.done:
                        break
                    endpoint_iterator = page.iterator

            if len(developers) < _DEVELOPER_PAGE_SIZE:
                break
            developer_offset += _DEVELOPER_PAGE_SIZE

    if configured == 0:
        raise RuntimeError("Lucie webhook endpoint was not found")
    return configured


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s - %(name)s] (%(levelname)s) %(message)s")
    count = configure_lucie_webhook_filters()
    logger.info("Restricted %d Lucie webhook endpoint(s)", count)
