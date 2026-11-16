import asyncio
from pathlib import Path

_DIRECTORY = Path("data")


async def write(html: str, date: str) -> Path:
    """Where a dry run leaves its digest, for the command line and the API alike."""
    path = _DIRECTORY / f"digest-{date}.html"

    def write_file() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    await asyncio.to_thread(write_file)

    return path
