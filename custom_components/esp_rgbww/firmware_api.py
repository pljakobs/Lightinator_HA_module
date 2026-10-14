"""Client for the Lightinator firmware version API."""

from __future__ import annotations

import posixpath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


def firmware_api_base_url(catalog_url: str) -> str:
    """Derive the API root beside the configured generated catalog."""
    parsed = urlsplit(catalog_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid firmware catalog URL")
    base_path = posixpath.dirname(parsed.path.rstrip("/")).rstrip("/")
    api_path = f"{base_path}/api" if base_path else "/api"
    return urlunsplit((parsed.scheme, parsed.netloc, api_path, "", ""))


def firmware_branch_from_version(version: str) -> str:
    """Extract the branch suffix from a firmware git-describe version."""
    parts = version.strip().split("-")
    return "-".join(parts[2:]).lower() if len(parts) >= 3 else "stable"


def firmware_version_key(version: str) -> tuple[int, int, int, int]:
    """Parse the numeric version/build tuple used by the version API."""
    parts = version.strip().lstrip("Vv").split("-")
    base = parts[0].split(".")
    try:
        major = int(base[0]) if len(base) > 0 else 0
        minor = int(base[1]) if len(base) > 1 else 0
        patch = int(base[2]) if len(base) > 2 else 0
    except ValueError:
        return (0, 0, 0, 0)
    build = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (major, minor, patch, build)


class FirmwareVersionApi:
    """Fetch branch, build type, and firmware choices from lightinator.de."""

    def __init__(self, session: aiohttp.ClientSession, catalog_url: str) -> None:
        self._session = session
        self._base_url = firmware_api_base_url(catalog_url)

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        async with self._session.get(
            f"{self._base_url}{path}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def branches(self, soc: str) -> list[str]:
        result = await self._get("/firmware/branches", {"soc": soc})
        if not isinstance(result, list):
            raise ValueError("Invalid firmware branches response")
        return [item for item in result if isinstance(item, str)]

    async def types(self, soc: str, branch: str) -> list[str]:
        result = await self._get(
            "/firmware/types", {"soc": soc, "branch": branch}
        )
        if not isinstance(result, list):
            raise ValueError("Invalid firmware types response")
        return [item for item in result if isinstance(item, str)]

    async def versions(
        self, soc: str, branch: str, build_type: str
    ) -> list[dict[str, Any]]:
        result = await self._get(
            "/firmware/versions",
            {"soc": soc, "branch": branch, "type": build_type},
        )
        if not isinstance(result, list):
            raise ValueError("Invalid firmware versions response")
        return [item for item in result if isinstance(item, dict)]