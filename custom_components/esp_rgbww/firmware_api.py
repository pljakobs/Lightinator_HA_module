"""Client for the Lightinator firmware version API."""

from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


def firmware_api_base_url(catalog_url: str) -> str:
    """Derive the API root beside the configured generated catalog."""
    parsed = urlsplit(catalog_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid firmware catalog URL")
    api_index = parsed.path.find("/api/")
    base_path = posixpath.dirname(parsed.path.rstrip("/")).rstrip("/")
    api_path = parsed.path[:api_index + 4] if api_index >= 0 else (f"{base_path}/api" if base_path else "/api")
    return urlunsplit((parsed.scheme, parsed.netloc, api_path, "", ""))


def firmware_identity(version: str, branch: str = "") -> tuple[str, str]:
    """Normalize known legacy and versioned identities without guessing a feed."""
    match = re.match(r"^[Vv]?(\d+)\.(\d+)(?:\.|-|$)", version)
    if not match:
        return ("", "")
    line = f"{int(match[1])}.{int(match[2])}"
    suffix = branch or re.sub(r"^[Vv]?\d+\.\d+(?:\.\d+)?-\d+-", "", version)
    qualified = re.fullmatch(r"(\d+\.\d+)[/-](develop|devel|testing|test|stable|prod|experimental)", suffix.lower())
    if qualified:
        if qualified[1] != line:
            return ("", "")
        suffix = qualified[2]
    stage = {"develop": "develop", "devel": "develop", "experimental": "develop", "test": "testing", "testing": "testing", "stable": "prod", "prod": "prod"}.get(suffix.lower(), "")
    return (line, stage)


def firmware_branch_from_version(version: str) -> str:
    """Return canonical line/stage for an unambiguous installed identity."""
    line, stage = firmware_identity(version)
    return f"{line}/{stage}" if line and stage else ""


def firmware_selection(installed_version: str, branch: str = "", release_line: str = "") -> tuple[str, str]:
    installed_line, installed_stage = firmware_identity(installed_version)
    selected = (branch or installed_stage).strip().lower()
    qualified = re.fullmatch(r"(\d+\.\d+)[/-](develop|devel|testing|test|stable|prod|experimental)", selected)
    if qualified:
        if release_line and release_line != qualified[1]:
            raise ValueError("Saved firmware release line and branch disagree")
        release_line, selected = qualified.groups()
    line = release_line or installed_line
    normalized_line, stage = firmware_identity(f"V{line}.0", selected)
    if not normalized_line or not stage:
        raise ValueError("Select an explicit firmware release line and stage")
    return normalized_line, stage


def firmware_version_key(version: str) -> tuple:
    """Parse the numeric version/build tuple used by the version API."""
    match = re.fullmatch(r"[Vv]?(\d+)\.(\d+)(?:\.(\d+))?(?:-([^+]+))?(?:\+.*)?", version.strip())
    if not match:
        return (-1, -1, -1, 0, ())
    prerelease = match[4]
    identifiers = prerelease.split(".") if prerelease else []
    legacy = re.match(r"^(\d+)(?:-|$)", prerelease or "")
    if legacy:
        identifiers = [legacy[1]]
    return (int(match[1]), int(match[2]), int(match[3] or 0), int(not prerelease),
            tuple((0, int(value)) if value.isdigit() else (1, value) for value in identifiers))


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
        choices = await self.options(soc)
        return sorted({f"{item['release_line']}/{item['stage']}" for item in choices})

    async def options(self, soc: str) -> list[dict[str, str]]:
        try:
            result = await self._get("/v2/firmware/options", {"soc": soc, "product": "controller"})
        except aiohttp.ClientResponseError as error:
            if error.status not in {404, 405}:
                raise
            records = await self._get("/firmware/versions", {"soc": soc})
            if not isinstance(records, list):
                raise ValueError("Invalid firmware versions response")
            choices = set()
            for record in records:
                if not isinstance(record, dict) or record.get("product", "controller") != "controller":
                    continue
                line, stage = firmware_identity(record.get("version", ""), record.get("branch", ""))
                if line and stage and record.get("type") in {"debug", "release"} and (soc != "esp8266" or line == "5.0"):
                    choices.add((line, stage, record["type"]))
            return [dict(zip(("release_line", "stage", "build_type"), item)) for item in sorted(choices)]
        if not isinstance(result, dict) or not isinstance(result.get("combinations"), list) or result.get("product", "controller") != "controller":
            raise ValueError("Invalid firmware options response")
        for item in result["combinations"]:
            if not isinstance(item, dict) or not re.fullmatch(r"\d+\.\d+", str(item.get("release_line", ""))) or item.get("stage") not in {"develop", "testing", "prod"} or item.get("build_type") not in {"debug", "release"}:
                raise ValueError("Invalid firmware option combination")
        return result["combinations"]

    async def types(self, soc: str, branch: str | None = None) -> list[str]:
        params = {"soc": soc}
        if branch:
            params["branch"] = branch
        result = await self._get("/firmware/types", params)
        if not isinstance(result, list):
            raise ValueError("Invalid firmware types response")
        return [item for item in result if isinstance(item, str)]

    async def versions(
        self, soc: str, branch: str, build_type: str
    ) -> list[dict[str, Any]]:
        identity = re.fullmatch(r"(\d+\.\d+)/(develop|testing|prod)", branch)
        if identity is None:
            raise ValueError("A canonical release line/stage is required")
        line, stage = identity.groups()
        try:
            result = await self._get("/v2/firmware/versions", {"soc": soc, "release_line": line, "stage": stage, "build_type": build_type, "product": "controller"})
        except aiohttp.ClientResponseError as error:
            if error.status not in {404, 405}:
                raise
            result = await self._get("/firmware/versions", {"soc": soc, "type": build_type})
        if not isinstance(result, list):
            raise ValueError("Invalid firmware versions response")
        candidates = []
        for item in result:
            if not isinstance(item, dict):
                continue
            if item.get("product", "controller") != "controller":
                continue
            candidate_line, candidate_stage = firmware_identity(item.get("version", ""), item.get("branch", ""))
            explicit_line = item.get("release_line", candidate_line)
            explicit_stage = item.get("stage", candidate_stage)
            explicit_type = item.get("build_type", item.get("type"))
            if explicit_line != candidate_line or (candidate_stage and explicit_stage != candidate_stage) or (item.get("type") and explicit_type != item["type"]):
                raise ValueError("Inconsistent firmware API identity")
            candidate_stage = explicit_stage
            if (candidate_line, candidate_stage) != (line, stage) or item.get("soc") != soc or item.get("type") != build_type:
                continue
            if soc == "esp8266" and line != "5.0":
                continue
            candidates.append({**item, "branch": branch, "release_line": line, "stage": stage, "product": "controller", "build_type": build_type})
        return sorted(candidates, key=lambda item: firmware_version_key(item["version"]), reverse=True)

    async def verify_upgrade(
        self, candidate: dict[str, Any], installed_version: str, installed_webapp_version: str
    ) -> dict[str, Any]:
        current = await self.versions(candidate["soc"], candidate["branch"], candidate["type"])
        refreshed = next((item for item in current if item.get("version") == candidate.get("version")
                          and item.get("id") == candidate.get("id")), None)
        if refreshed is None or any(refreshed.get(key) != candidate.get(key) for key in ("url", "sha256", "size")):
            raise ValueError("Selected firmware has changed or is no longer available")
        installed_line, _ = firmware_identity(installed_version)
        if not installed_line:
            raise ValueError("Installed identity is unknown; cannot verify an upgrade path")
        source_line = tuple(int(component) for component in installed_line.split("."))
        target_line = tuple(int(component) for component in refreshed["release_line"].split("."))
        if target_line < source_line:
            raise ValueError("Cross-line downgrades require a ConfigDB schema compatibility review")
        return refreshed