#!/usr/bin/env python3
"""Restrict Lucie's outgoing webhook endpoints to the product events it consumes.

This runs as a one-off task inside the deployed backend environment. It deliberately
uses the backend's existing database and Svix credentials instead of a human admin
password, so production automation does not depend on mutable dashboard credentials.
"""

import logging
import re

from svix.api.errors.http_error import HttpError

from app.database import SessionLocal
from app.services import developer_service
from app.services.outgoing_webhooks import svix as svix_service

logger = logging.getLogger(__name__)

_DEVELOPER_PAGE_SIZE = 250
_LUCIE_ENDPOINT_PATTERN = re.compile(r"https://api\.getlucie\.ai/(?:api/v1/)?webhooks/ow/?")
_LUCIE_FILTER_TYPES = ["sleep.created", "sleep.updated", "connection.created", "sync.completed"]


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
                try:
                    # Read every page before patching, so a 404 (no Svix application) skips the developer
                    endpoints = list(svix_service.iter_endpoints(str(developer.id)))
                except HttpError as exc:
                    if exc.status_code == 404:
                        continue
                    raise

                for endpoint in endpoints:
                    if _LUCIE_ENDPOINT_PATTERN.fullmatch(endpoint.url) is None:
                        continue
                    updated = svix_service.patch_endpoint(
                        str(developer.id),
                        endpoint.id,
                        filter_types=_LUCIE_FILTER_TYPES,
                    )
                    if sorted(updated.event_types or []) != sorted(_LUCIE_FILTER_TYPES):
                        raise RuntimeError("Svix did not persist the required Lucie event filters")
                    configured += 1

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
