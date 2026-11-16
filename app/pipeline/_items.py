from app.models import DigestEventPlacement
from app.models import ItemKind
from app.models import StoredItem

# Reports first, then what people said about them.
KIND_ORDER: dict[ItemKind, int] = {
    ItemKind.PRIMARY: 0,
    ItemKind.MEDIA: 1,
    ItemKind.COMMENTARY: 2,
    ItemKind.DISCUSSION: 3,
}
REPORT_KINDS = frozenset({ItemKind.PRIMARY, ItemKind.MEDIA})


def engagement_value(item: StoredItem) -> int:
    engagement = item.engagement

    if engagement is None:
        return 0

    return (
        (engagement.likes or 0)
        + (engagement.score or 0)
        + (engagement.comments or 0)
        + (engagement.shares or 0)
    )


def chosen_items(placement: DigestEventPlacement) -> list[StoredItem]:
    """The items the selection picked for the digest, or every item of the event when
    none of the picked ones is left."""
    wanted = set(placement.item_ids)
    chosen = [item for item in placement.event.items if item.id in wanted]

    return chosen if chosen else list(placement.event.items)
