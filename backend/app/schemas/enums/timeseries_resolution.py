from datetime import timedelta
from enum import StrEnum


class TimeseriesResolution(StrEnum):
    """Bucket width for a ``/timeseries`` read.

    RAW returns every stored sample. Every other value downsamples
    server-side: samples are binned into fixed windows and one value per
    (bucket, series type, data source) is returned, aggregated by the
    series type's own method (``AGGREGATION_METHOD_BY_TYPE``) — summed for
    cumulative types like steps, averaged for rates like heart rate.
    """

    RAW = "raw"
    ONE_MINUTE = "1min"
    FIVE_MINUTES = "5min"
    FIFTEEN_MINUTES = "15min"
    ONE_HOUR = "1hour"


# Bucket width per downsampling resolution. RAW is absent intentionally —
# it does no bucketing. Add an entry here when adding a resolution.
RESOLUTION_BUCKET: dict[TimeseriesResolution, timedelta] = {
    TimeseriesResolution.ONE_MINUTE: timedelta(minutes=1),
    TimeseriesResolution.FIVE_MINUTES: timedelta(minutes=5),
    TimeseriesResolution.FIFTEEN_MINUTES: timedelta(minutes=15),
    TimeseriesResolution.ONE_HOUR: timedelta(hours=1),
}


def bucket_width(resolution: TimeseriesResolution | None) -> timedelta | None:
    """Bucket width for ``resolution``, or ``None`` when it does not downsample."""
    if resolution is None:
        return None
    return RESOLUTION_BUCKET.get(resolution)
