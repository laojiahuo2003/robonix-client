"""Compute-node telemetry bridge (Route B).

Connects a browser panel DIRECTLY to the `linux_health` primitive's own
`robonix/primitive/health/stream` contract (resolved through Atlas), bypassing
the frozen Soma/Vitals chain. When the deploy does NOT include the primitive,
the adapter reports `source=unavailable` and stops, so the browser keeps the
Compute Node panel hidden - restoring the original UI. The panel only appears
once a frame is streamed from the real primitive.

Only the fake-data source was changed on the backend side; nothing here wires
into Soma or Vitals.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import grpc
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .proto import health_pb2
from .transport import ClientSettings, discover_endpoint, grpc_channel

router = APIRouter()

CONTRACT_HEALTH_STREAM = "robonix/primitive/health/stream"
CONTRACT_HEALTH_STATE = "robonix/primitive/health/state"

HEALTH_STREAM_PATH = (
    "/robonix.contracts.RobonixPrimitiveHealthStream/StreamHealthState"
)
HEALTH_STATE_PATH = "/robonix.contracts.RobonixPrimitiveHealthState/GetHealthState"

# Reading names emitted by the deployment manifest's linux_health config.
CPU_READING = "body/compute_node/cpu"
POWER_READING = "body/compute_node/input_power"
# The fake-sys linux_health provider that streams compute_node telemetry. Other
# deployments (e.g. tiago_health) register the SAME contract id, so hint Atlas
# to the one that owns the compute node readings.
HEALTH_PROVIDER = "linux_health"


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, grpc.aio.AioRpcError):
        return f"gRPC {exc.code().name}: {exc.details()}"
    return str(exc) or exc.__class__.__name__


def _positive(value: float) -> float | None:
    """Return the value, or None when the primitive's -1 'unknown' sentinel."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def health_state_to_sample(state: health_pb2.HealthState) -> dict[str, Any]:
    """Project one HealthState frame into the browser's flat sample shape."""
    readings = [
        {
            "name": reading.name,
            "tempC": _positive(reading.temp_c),
            "voltage": _positive(reading.voltage),
            "currentA": _positive(reading.current_a),
            "batteryPercent": _positive(reading.battery_percent),
        }
        for reading in state.readings
    ]

    def pick(reading_name: str, key: str) -> float | None:
        for reading in readings:
            if reading["name"] == reading_name and reading[key] is not None:
                return reading[key]
        return None

    cpu_temp = pick(CPU_READING, "tempC")
    voltage = pick(POWER_READING, "voltage")
    if voltage is None and _positive(state.voltage) is not None:
        voltage = float(state.voltage)
    current_a = pick(POWER_READING, "currentA")

    return {
        "ts": int(time.time() * 1000),
        "cpuTemp": cpu_temp,
        "voltage": voltage,
        "current": current_a,
        "raw": readings,
    }


async def _stream_primitive(
    settings: ClientSettings,
) -> AsyncIterator[dict[str, Any]]:
    """Relay live HealthState frames from the linux_health primitive."""
    endpoint = await discover_endpoint(
        settings.atlas_endpoint, CONTRACT_HEALTH_STREAM, provider_hint=HEALTH_PROVIDER
    )
    async with grpc_channel(endpoint) as channel:
        call = channel.unary_stream(
            HEALTH_STREAM_PATH,
            request_serializer=health_pb2.StreamHealthState_Request.SerializeToString,
            response_deserializer=health_pb2.HealthState.FromString,
        )
        async for state in call(health_pb2.StreamHealthState_Request()):
            yield {
                "type": "sample",
                "source": "primitive",
                "data": health_state_to_sample(state),
            }


async def stream_health_events(settings: ClientSettings) -> AsyncIterator[dict[str, Any]]:
    """Yield browser-ready events. Reports `unavailable` (and stops) when the
    deployment does not include the linux_health primitive, so the browser
    keeps the Compute Node panel hidden."""
    yield {"type": "accepted", "contract": CONTRACT_HEALTH_STREAM}
    try:
        async for event in _stream_primitive(settings):
            yield event
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Primitive not discovered (or unreachable) in this deployment -> tell the
        # browser the panel has no live source; it stays hidden like the original UI.
        yield {
            "type": "source",
            "source": "unavailable",
            "error": f"linux_health primitive unavailable: {_error_text(exc)}",
        }


@router.websocket("/ws/health")
async def health_ws(ws: WebSocket) -> None:
    await ws.accept()
    try:
        payload = await ws.receive_json()
        settings = ClientSettings.from_payload(payload.get("settings"))
        await ws.send_json({"type": "source", "source": "connecting"})
        async for event in stream_health_events(settings):
            await ws.send_json(event)
    except WebSocketDisconnect:
        return
    except grpc.aio.AioRpcError as exc:
        await _send_error(ws, f"gRPC {exc.code().name}: {exc.details()}")
    except Exception as exc:
        await _send_error(ws, str(exc))


async def _send_error(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_json({"type": "error", "error": message})
    except (RuntimeError, WebSocketDisconnect):
        pass