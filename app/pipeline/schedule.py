from datetime import UTC
from datetime import datetime
from datetime import tzinfo
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from app import logging
from app.models import Model
from app.models import Profile

logger = logging.get_logger(__name__)

_REASON_DELIVERY_HOUR = "delivery_hour"
_REASON_NOT_DELIVERY_HOUR = "not_delivery_hour"
_REASON_ALREADY_SENT = "already_sent_today"


class ScheduleDecision(Model):
    send: bool
    local_date: str
    reason: str


def _zone_for(timezone: str) -> tzinfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        # NOTE: zoneinfo has no non-raising lookup; the profile is owner-edited, so an
        # unknown zone degrades to UTC rather than stopping every delivery.
        logger.warning("Unknown timezone; using UTC.", extra={"timezone": timezone})

        return UTC


def should_send_now(profile: Profile, now: datetime) -> ScheduleDecision:
    """Called hourly: sends at the owner's delivery hour, at most once per local day."""
    local = now.astimezone(_zone_for(profile.timezone))
    local_date = local.strftime("%Y-%m-%d")

    if local.hour != profile.delivery_hour:
        return ScheduleDecision(
            send=False, local_date=local_date, reason=_REASON_NOT_DELIVERY_HOUR
        )

    if profile.last_digest_date == local_date:
        return ScheduleDecision(
            send=False, local_date=local_date, reason=_REASON_ALREADY_SENT
        )

    return ScheduleDecision(
        send=True, local_date=local_date, reason=_REASON_DELIVERY_HOUR
    )
