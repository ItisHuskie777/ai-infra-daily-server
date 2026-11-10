from datetime import UTC
from datetime import datetime
from datetime import tzinfo
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from app import logging
from app.models import Model
from app.models import Profile

logger = logging.get_logger(__name__)

_REASON_DUE = "due"
_REASON_BEFORE_DELIVERY_HOUR = "before_delivery_hour"
_REASON_ALREADY_SENT = "already_sent_today"


class ScheduleDecision(Model):
    send: bool
    local_date: str
    reason: str


def zone_for(timezone: str) -> tzinfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        # NOTE: zoneinfo has no non-raising lookup; the profile is owner-edited, so an
        # unknown zone degrades to UTC rather than stopping every delivery.
        logger.warning("Unknown timezone; using UTC.", extra={"timezone": timezone})

        return UTC


def should_send_now(profile: Profile, now: datetime) -> ScheduleDecision:
    """Called hourly: the digest is due from the owner's delivery hour until it has
    gone out that local day, so a failed or missed run is retried the next hour and a
    delivery hour skipped by a daylight-saving change is caught up."""
    local = now.astimezone(zone_for(profile.timezone))
    local_date = local.strftime("%Y-%m-%d")

    if profile.last_digest_date == local_date:
        return ScheduleDecision(
            send=False, local_date=local_date, reason=_REASON_ALREADY_SENT
        )

    if local.hour < profile.delivery_hour:
        return ScheduleDecision(
            send=False, local_date=local_date, reason=_REASON_BEFORE_DELIVERY_HOUR
        )

    return ScheduleDecision(send=True, local_date=local_date, reason=_REASON_DUE)


def start_of_local_day(timezone: str, now: datetime) -> datetime:
    local = now.astimezone(zone_for(timezone))

    return local.replace(hour=0, minute=0, second=0, microsecond=0)
