# SPDX-License-Identifier: MulanPSL-2.0
"""Perception data access for the web client.

The Perception page visualises sensor + spatial data that a Robonix
deployment exposes through Atlas.  Two transports are reachable from the
host-side client:

  * MCP  (transport=3)  -- one-shot tool contracts such as
        ``camera/snapshot``, ``lidar/snapshot`` and the ``scene`` semantic
        RPCs.  We speak the MCP streamable-HTTP protocol directly (JSON-RPC
        over HTTP + SSE) so the client needs no extra runtime dependency.
  * gRPC (transport=1)  -- streaming contracts, already handled elsewhere.

ROS 2 topics (transport=2) are *not* reachable from the host side, so
anything only published there is simply reported as unavailable rather than
surfacing a broken tile.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from .transport import CONSUMER_ID, grpc_channel, query_atlas

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_INFO = {"name": "robonix-client", "version": "1.0.0"}

# Contract IDs used by the Perception page.  ``snapshot`` tools are the
# MCP-facing, one-shot form of the underlying ROS 2 sensor streams.
CONTRACT_CAMERA_RGB = "robonix/primitive/camera/snapshot"
CONTRACT_CAMERA_DEPTH = "robonix/primitive/camera/depth_snapshot"
CONTRACT_LIDAR = "robonix/primitive/lidar/snapshot"
CONTRACT_SCENE_ROBOT = "robonix/system/scene/get_robot_context"
CONTRACT_SCENE_OBJECTS = "robonix/system/scene/list_objects"
CONTRACT_SCENE_REGIONS = "robonix/system/scene/list_regions"

# The occupancy-grid base map is *not* reachable through Atlas: the grid itself
# is published on ROS 2 and only surfaced as a rendered PNG by the robot-local
# "map & rooms" HTTP service (the same one behind the :50107/user UI). We read
# that service directly, deriving its host from the Atlas endpoint and treating
# its port as a deployment default in the same spirit as DEFAULT_ATLAS /
# DEFAULT_LIAISON_PORT -- overridable without touching the robot side.
MAP_UI_PORT = int(os.environ.get("ROBONIX_CLIENT_MAP_UI_PORT", "50107"))

@dataclass(frozen=True)
class _PerceptionResource:
    """One sensor/scene resource the UI can show: a contract + allowed transports."""

    key: str
    contract_id: str
    transports: tuple[int, ...] = (3,)


@dataclass(frozen=True)
class _PerceptionTile:
    """A UI tile built from one or more independently-degrading resources."""

    id: str
    group: str
    resources: tuple[_PerceptionResource, ...]


# Declarative registry in the order the UI lays tiles out.  A tile renders when
# *any* of its resources is reachable, so a partial deployment (a scene without
# room regions, a robot without a depth camera) still shows what it has.  Each
# resource lists the transports the host-side client can consume; a contract
# only published on ROS 2 (transport=2) is simply reported unavailable.
PERCEPTION_TILES: tuple[_PerceptionTile, ...] = (
    _PerceptionTile("camera", "sensors", (_PerceptionResource("rgb", CONTRACT_CAMERA_RGB),)),
    _PerceptionTile("depth", "sensors", (_PerceptionResource("depth", CONTRACT_CAMERA_DEPTH),)),
    _PerceptionTile("lidar", "sensors", (_PerceptionResource("scan", CONTRACT_LIDAR),)),
    _PerceptionTile(
        "scene",
        "map",
        (
            _PerceptionResource("robot", CONTRACT_SCENE_ROBOT),
            _PerceptionResource("objects", CONTRACT_SCENE_OBJECTS),
            _PerceptionResource("regions", CONTRACT_SCENE_REGIONS),
        ),
    ),
)


class _McpSession:
    __slots__ = ("endpoint", "session_id")

    def __init__(self, endpoint: str, session_id: str | None) -> None:
        self.endpoint = endpoint
        self.session_id = session_id


# Endpoint + session cache keyed by (atlas, contract_id).  The MCP handshake
# is two round-trips; polling a snapshot at a few fps should not re-pay it.
_session_cache: dict[tuple[str, str], _McpSession] = {}
_session_lock = threading.Lock()


def _post_json(url: str, payload: dict[str, Any], session_id: str | None = None) -> tuple[str, str | None]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        body = resp.read().decode("utf-8")
        return body, resp.headers.get("Mcp-Session-Id")


def _sse_json(body: str) -> list[Any]:
    """Parse the JSON-RPC messages out of a streamable-HTTP SSE body."""
    messages: list[Any] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        messages.append(json.loads(payload))
                    except json.JSONDecodeError:
                        continue
    return messages


def _handshake(endpoint: str) -> _McpSession:
    body, session_id = _post_json(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": MCP_CLIENT_INFO,
            },
        },
    )
    # A missing session id is tolerated for servers that keep stateless
    # endpoints; the initialized notification is best-effort regardless.
    try:
        _post_json(
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
        )
    except (urllib.error.URLError, OSError):
        pass
    return _McpSession(endpoint, session_id)


def _call_tool_sync(sess: _McpSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body, _sid = _post_json(
        sess.endpoint,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        sess.session_id,
    )
    for message in _sse_json(body):
        if message.get("id") != 2:
            continue
        result = message.get("result")
        if result is None:
            error = message.get("error", {})
            raise RuntimeError(f"MCP {tool}: {error}")
        if result.get("isError"):
            raise RuntimeError(f"MCP {tool}: {result.get('content')}")
        content = result.get("content") or []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
        return result.get("structuredContent") or {}
    raise RuntimeError(f"MCP {tool}: no tool result in response")


async def _discover_mcp_endpoint(atlas_endpoint: str, contract_id: str) -> str:
    """Return the MCP HTTP endpoint for ``contract_id``'s provider."""
    providers = await query_atlas(atlas_endpoint, contract_id=contract_id, transport=3)
    for provider in providers:
        for cap in provider.capabilities:
            if cap.contract_id != contract_id or cap.transport != 3:
                continue
            import atlas_pb2  # noqa: PLC0415 -- proto dir added by transport import

            req = atlas_pb2.ConnectCapabilityRequest(
                consumer_id=CONSUMER_ID,
                provider_id=provider.id,
                contract_id=contract_id,
                transport=3,
            )
            channel = grpc_channel(atlas_endpoint)
            call = channel.unary_unary(
                "/robonix.atlas.Atlas/ConnectCapability",
                request_serializer=atlas_pb2.ConnectCapabilityRequest.SerializeToString,
                response_deserializer=atlas_pb2.ConnectCapabilityResponse.FromString,
            )
            resp = await call(req, timeout=4.0)
            return _rewrite_loopback(resp.endpoint.rstrip("/"), atlas_endpoint)
    raise RuntimeError(f"no MCP provider for {contract_id}")


async def mcp_call(
    atlas_endpoint: str,
    contract_id: str,
    *,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call an MCP tool exposed by the provider of ``contract_id``.

    The MCP tool name is the contract id's leaf segment (enforced by the
    provider framework), so callers only need to pass a non-default ``tool``
    when the convention does not line up.
    """
    tool = tool or contract_id.rsplit("/", 1)[-1]
    arguments = arguments or {}
    key = (atlas_endpoint, contract_id)
    sess = _session_cache.get(key)
    if sess is None:
        endpoint = await _discover_mcp_endpoint(atlas_endpoint, contract_id)
        sess = await asyncio.to_thread(_handshake, endpoint)
        with _session_lock:
            _session_cache[key] = sess
    try:
        return await asyncio.to_thread(_call_tool_sync, sess, tool, arguments)
    except (urllib.error.URLError, OSError, RuntimeError):
        # Session likely expired or endpoint moved; drop the cache so the next
        # poll re-discovers + re-handshakes instead of pinning a dead session.
        with _session_lock:
            _session_cache.pop(key, None)
        raise


async def _resource_reachable(atlas_endpoint: str, resource: _PerceptionResource) -> bool:
    """True if Atlas advertises ``resource``'s contract over an allowed transport."""
    for transport in resource.transports:
        try:
            providers = await query_atlas(
                atlas_endpoint, contract_id=resource.contract_id, transport=transport
            )
        except Exception:
            continue
        if any(
            cap.contract_id == resource.contract_id and cap.transport == transport
            for provider in providers
            for cap in provider.capabilities
        ):
            return True
    return False


def _atlas_host(atlas_endpoint: str) -> str:
    """The robot host implied by the Atlas endpoint the client is configured for."""
    target = (atlas_endpoint or "").strip()
    parsed = urlparse(target if "://" in target else f"grpc://{target}")
    return parsed.hostname or "127.0.0.1"


def _map_ui_base(atlas_endpoint: str) -> str:
    """HTTP base of the robot's map/rooms service, host derived from Atlas."""
    return f"http://{_atlas_host(atlas_endpoint)}:{MAP_UI_PORT}"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})


def _rewrite_loopback(endpoint: str, atlas_endpoint: str) -> str:
    """Point a loopback-advertised endpoint at the robot host the client used.

    Robot-side providers often advertise their MCP endpoint as 127.0.0.1 even
    though they bind 0.0.0.0. From the same machine that loopback is fine, but
    a client running on another LAN host would dial its *own* loopback and
    never reach the robot. Only loopback hosts are rewritten; a provider that
    already advertises a routable address is left untouched.
    """
    parsed = urlparse(endpoint)
    if parsed.hostname not in _LOOPBACK_HOSTS:
        return endpoint
    host = _atlas_host(atlas_endpoint)
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def _fetch_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def map_occupancy(atlas_endpoint: str) -> dict[str, Any]:
    """The live occupancy-grid PNG + pose, straight from the map HTTP service.

    This is the one spatial source the host-side client cannot reach through
    Atlas: the grid is published on ROS 2 and only surfaced as a rendered PNG
    by the robot-local map service, so we read that service over HTTP. The
    returned dict carries ``origin_x``/``origin_y``/``resolution`` so the UI
    can align it with the scene overlays in the shared ``map`` frame.
    """
    state = await asyncio.to_thread(_fetch_json, f"{_map_ui_base(atlas_endpoint)}/api/state")
    occupancy = state.get("occupancy") or {}
    if not occupancy.get("png_b64"):
        raise RuntimeError("map service reports no occupancy grid yet")
    return occupancy


async def map_reachable(atlas_endpoint: str) -> bool:
    """True when the robot-local map service can hand us an occupancy grid."""
    try:
        return bool((await map_occupancy(atlas_endpoint)).get("png_b64"))
    except Exception:
        return False


async def perception_availability(atlas_endpoint: str) -> dict[str, Any]:
    """Per-resource reachability for every perception tile.

    A tile is ``available`` when *any* of its resources is reachable, so a
    partial deployment (a scene without room regions, for example) still
    renders.  ``resources`` lets the UI degrade per layer instead of per tile;
    ``groups`` maps each view group to its tile ids.
    """
    tiles: dict[str, bool] = {}
    resources: dict[str, dict[str, bool]] = {}
    groups: dict[str, list[str]] = {}
    for tile in PERCEPTION_TILES:
        per_resource: dict[str, bool] = {}
        for resource in tile.resources:
            per_resource[resource.key] = await _resource_reachable(atlas_endpoint, resource)
        resources[tile.id] = per_resource
        tiles[tile.id] = any(per_resource.values())
        groups.setdefault(tile.group, []).append(tile.id)
    # The occupancy base map is served by the robot-local HTTP service rather
    # than an Atlas contract; probe it separately and fold it into the scene
    # tile as a toggleable layer. A deployment that has the map service but no
    # scene RPC contracts still gets a scene tile (showing just the grid).
    resources.setdefault("scene", {})["map"] = await map_reachable(atlas_endpoint)
    tiles["scene"] = bool(tiles.get("scene") or resources["scene"]["map"])
    return {"tiles": tiles, "resources": resources, "groups": groups}


async def camera_rgb(atlas_endpoint: str) -> dict[str, Any]:
    return await mcp_call(atlas_endpoint, CONTRACT_CAMERA_RGB)


async def camera_depth(atlas_endpoint: str) -> dict[str, Any]:
    return await mcp_call(atlas_endpoint, CONTRACT_CAMERA_DEPTH)


async def lidar_scan(atlas_endpoint: str) -> dict[str, Any]:
    return await mcp_call(atlas_endpoint, CONTRACT_LIDAR)


async def scene_snapshot(atlas_endpoint: str) -> dict[str, Any]:
    """Bundle the scene reads into one payload, tolerating partial coverage.

    Each resource is read independently; a resource whose provider is missing
    or errors becomes ``None`` so the map tile still renders the reachable
    parts (objects + robot without room regions, for example).
    """

    async def read(contract_id: str) -> dict[str, Any] | None:
        try:
            return await mcp_call(atlas_endpoint, contract_id)
        except Exception:
            return None

    robot, objects, regions = await asyncio.gather(
        read(CONTRACT_SCENE_ROBOT),
        read(CONTRACT_SCENE_OBJECTS),
        read(CONTRACT_SCENE_REGIONS),
    )
    return {"robot": robot, "objects": objects, "regions": regions}
