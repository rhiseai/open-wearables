"""
Tests for DataSourceRepository.

Regression coverage for the ``data_source.source`` column width: Apple
HealthKit tags on-device data with a source bundle identifier of the form
``com.apple.health.<UUID>`` (53 chars). This overflowed the previous
``VARCHAR(50)`` and aborted the entire SDK import batch with
``StringDataRightTruncation``, so no Apple sleep/records were ever saved.
The column is now ``VARCHAR(100)``.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataSource, ProviderPriority, User
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import ProviderName
from tests.factories import UserFactory

# Realistic Apple HealthKit source bundle id: "com.apple.health." + a UUID.
APPLE_HEALTH_SOURCE = "com.apple.health.ED447642-08FD-4E45-AF20-633C02C83170"


class TestDataSourceRepository:
    """Test suite for DataSourceRepository."""

    def test_apple_health_source_bundle_id_persists(self, db: Session) -> None:
        """A 53-char Apple source bundle id imports without truncation.

        Exercises ``ensure_data_source`` (the SDK import path that failed) and
        asserts the full identifier round-trips — this would raise
        ``StringDataRightTruncation`` against the old ``VARCHAR(50)`` column.
        """
        # Arrange: a source longer than the old 50-char limit.
        assert len(APPLE_HEALTH_SOURCE) > 50
        user = UserFactory()
        repo = DataSourceRepository(DataSource)

        # Act
        created = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        db.commit()
        db.expire_all()

        # Assert: stored intact, retrievable by its full identity.
        assert created.source == APPLE_HEALTH_SOURCE
        stored = repo.get_by_identity(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        assert stored is not None
        assert stored.source == APPLE_HEALTH_SOURCE
        assert len(stored.source) == len(APPLE_HEALTH_SOURCE)

    def test_parallel_creation_reuses_one_data_source(self, engine: Any, session_factory: Any) -> None:
        """Parallel SDK batches atomically resolve the same data-source identity."""
        user_id = uuid4()
        start = Barrier(2)

        with session_factory() as session:
            provider_priority_existed = (
                session.query(ProviderPriority).filter(ProviderPriority.provider == ProviderName.APPLE).first()
                is not None
            )
            session.add(User(id=user_id, email=f"{user_id}@example.com"))
            session.commit()

        def ensure_data_source() -> tuple[str, str | None]:
            with session_factory() as session:
                repo = DataSourceRepository(DataSource)
                get_by_identity = repo.get_by_identity
                first_lookup = True

                def synchronized_lookup(*args: Any, **kwargs: Any) -> DataSource | None:
                    nonlocal first_lookup
                    result = get_by_identity(*args, **kwargs)
                    if first_lookup:
                        first_lookup = False
                        assert result is None
                        start.wait()
                    return result

                repo.get_by_identity = synchronized_lookup  # type: ignore[method-assign]
                resolved = repo.ensure_data_source(
                    session,
                    user_id=user_id,
                    provider=ProviderName.APPLE,
                    device_model="Watch7,5",
                    source=APPLE_HEALTH_SOURCE,
                )
                return str(resolved.id), resolved.source

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: ensure_data_source(), range(2)))

            assert results[0] == results[1]
            with session_factory() as session:
                stored = session.query(DataSource).filter(DataSource.user_id == user_id).all()
                assert len(stored) == 1
        finally:
            with session_factory() as session:
                session.query(DataSource).filter(DataSource.user_id == user_id).delete()
                session.query(User).filter(User.id == user_id).delete()
                if not provider_priority_existed:
                    session.query(ProviderPriority).filter(ProviderPriority.provider == ProviderName.APPLE).delete()
                session.commit()
