"""JSON-RPC helpers for the Lightinator WebSocket API."""

from __future__ import annotations

import hashlib
from typing import Any

import aiohttp


class RpcError(Exception):
    """A JSON-RPC error returned by the device."""

    def __init__(
        self,
        code: int | None,
        message: str,
        challenge: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.challenge = challenge


class RpcAuthenticationError(RpcError):
    """Authentication failed or the device requires missing credentials."""


async def receive_rpc_response(
    ws: aiohttp.ClientWebSocketResponse, request_id: int
) -> dict[str, Any]:
    """Wait for one response, ignoring notifications received in the meantime."""
    while True:
        message = await ws.receive(timeout=10)
        if message.type == aiohttp.WSMsgType.TEXT:
            try:
                payload = message.json()
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("id") == request_id:
                return payload
        elif message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            raise ConnectionError("Lightinator WebSocket closed")


async def exchange_rpc(
    ws: aiohttp.ClientWebSocketResponse,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Send one JSON-RPC request and return its response envelope."""
    await ws.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )
    return await receive_rpc_response(ws, request_id)


async def authenticate_websocket(
    ws: aiohttp.ClientWebSocketResponse, password: str
) -> None:
    """Complete the firmware's one-shot SHA-256 challenge handshake."""
    response = await exchange_rpc(ws, 1, "authenticate", {})
    error = response.get("error")
    if not isinstance(error, dict):
        return

    challenge = error.get("challenge")
    if error.get("code") != -32001 or not isinstance(challenge, str):
        raise RpcAuthenticationError(
            error.get("code"), str(error.get("message", "Authentication failed"))
        )

    digest = hashlib.sha256(f"{challenge}:{password}".encode()).hexdigest()
    response = await exchange_rpc(ws, 2, "authenticate", {"hash": digest})
    error = response.get("error")
    if isinstance(error, dict):
        raise RpcAuthenticationError(
            error.get("code"),
            str(error.get("message", "Authentication failed")),
            error.get("challenge") if isinstance(error.get("challenge"), str) else None,
        )

    result = response.get("result")
    if not isinstance(result, dict) or result.get("authenticated") is not True:
        raise RpcAuthenticationError(None, "Invalid authentication response")


async def websocket_request(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    password: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open an authenticated WebSocket and make one JSON-RPC request."""
    url = f"ws://{host}:{port}/ws"
    async with session.ws_connect(url, heartbeat=30) as ws:
        await authenticate_websocket(ws, password)
        response = await exchange_rpc(ws, 3, method, params or {})

    error = response.get("error")
    if isinstance(error, dict):
        raise RpcError(
            error.get("code"), str(error.get("message", "JSON-RPC request failed"))
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise RpcError(None, "Invalid JSON-RPC response")
    return result