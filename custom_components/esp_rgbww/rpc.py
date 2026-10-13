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


def authentication_hash(challenge: str, password: str) -> str:
    """Return the firmware's SHA-256 challenge response."""
    return hashlib.sha256(f"{challenge}:{password}".encode()).hexdigest()


def _response_result(response: dict[str, Any]) -> dict[str, Any]:
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message", "JSON-RPC request failed"))
        challenge = error.get("challenge")
        error_type = RpcAuthenticationError if code == -32001 else RpcError
        raise error_type(
            code,
            message,
            challenge if isinstance(challenge, str) else None,
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise RpcError(None, "Invalid JSON-RPC response")
    return result


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
    ws: aiohttp.ClientWebSocketResponse,
    password: str,
    challenge: str,
    request_id: int,
) -> None:
    """Answer a challenge returned by the requested RPC method."""
    response = await exchange_rpc(
        ws,
        request_id,
        "authenticate",
        {"hash": authentication_hash(challenge, password)},
    )
    result = _response_result(response)
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
    """Open a plain WebSocket and authenticate only if the RPC requires it."""
    url = f"ws://{host}:{port}/ws"
    async with session.ws_connect(url, heartbeat=30) as ws:
        response = await exchange_rpc(ws, 1, method, params or {})
        try:
            return _response_result(response)
        except RpcAuthenticationError as err:
            if not err.challenge:
                raise
            await authenticate_websocket(ws, password, err.challenge, 2)
            response = await exchange_rpc(ws, 3, method, params or {})
            return _response_result(response)