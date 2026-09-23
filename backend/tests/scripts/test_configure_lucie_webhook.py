from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from scripts import configure_lucie_webhook


def _mock_database(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    db = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = db
    monkeypatch.setattr(configure_lucie_webhook, "SessionLocal", MagicMock(return_value=session))
    return db


def test_configures_only_exact_lucie_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _mock_database(monkeypatch)
    developer = SimpleNamespace(id=uuid4())
    endpoints = [
        SimpleNamespace(id="ep_lucie", url="https://api.getlucie.ai/api/v1/webhooks/ow"),
        SimpleNamespace(id="ep_other", url="https://example.com/webhooks/ow"),
    ]
    page = SimpleNamespace(data=endpoints, done=True, iterator=None)
    updated = SimpleNamespace(event_types=["connection.created", "sleep.updated", "sleep.created", "sync.completed"])

    monkeypatch.setattr(configure_lucie_webhook.svix_service, "is_enabled", MagicMock(return_value=True))
    get_all = MagicMock(return_value=[developer])
    monkeypatch.setattr(configure_lucie_webhook.developer_service.crud, "get_all", get_all)
    list_endpoints = MagicMock(return_value=page)
    monkeypatch.setattr(configure_lucie_webhook.svix_service, "list_endpoints", list_endpoints)
    patch_endpoint = MagicMock(return_value=updated)
    monkeypatch.setattr(configure_lucie_webhook.svix_service, "patch_endpoint", patch_endpoint)

    assert configure_lucie_webhook.configure_lucie_webhook_filters() == 1
    get_all.assert_called_once_with(db, filters={}, offset=0, limit=250, sort_by="id")
    list_endpoints.assert_called_once()
    patch_endpoint.assert_called_once_with(
        str(developer.id),
        "ep_lucie",
        filter_types=["sleep.created", "sleep.updated", "connection.created", "sync.completed"],
    )


def test_fails_when_lucie_endpoint_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_database(monkeypatch)
    monkeypatch.setattr(configure_lucie_webhook.svix_service, "is_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(configure_lucie_webhook.developer_service.crud, "get_all", MagicMock(return_value=[]))

    with pytest.raises(RuntimeError, match="Lucie webhook endpoint was not found"):
        configure_lucie_webhook.configure_lucie_webhook_filters()
