from datetime import datetime

from pydantic import BaseModel


class MetricCount(BaseModel):
    """A single count metric."""

    count: int


class DataPointsInfo(BaseModel):
    """Data points information.

    ``count`` is the live (hot) ``data_point_series`` table; ``archived`` is the separate archive
    table. Both are approximate on a cold cache / from planner statistics.
    """

    count: int
    archived: int


class EventRecordsInfo(BaseModel):
    """Event record counts with a breakdown by category."""

    count: int
    workouts: int
    sleep: int
    menstrual_cycles: int


class ProviderConnectionCount(BaseModel):
    provider: str
    count: int


class ProviderAdoption(BaseModel):
    """One provider's reach: users holding it, and users who joined recently.

    ``new_users`` counts first connections inside the requested window, so a
    consumer can render "total (+N this week)" without a second call.
    """

    provider: str
    total_users: int
    new_users: int


class ConnectionAdoptionResponse(BaseModel):
    """Per-provider adoption for every provider with an active connection."""

    since: datetime
    providers: list[ProviderAdoption]


class ConnectionsCoverage(BaseModel):
    users_with_active: int
    users_with_multi_active: int
    top_providers: list[ProviderConnectionCount]


class SystemInfoResponse(BaseModel):
    """Dashboard system information response."""

    total_users: MetricCount
    active_conn: MetricCount
    data_points: DataPointsInfo
    event_records: EventRecordsInfo
    connections_coverage: ConnectionsCoverage
