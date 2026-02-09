"""
Helios SBC Telemetry Service
─────────────────────────────
Flask API + WebSocket streaming for MAVSDK drone telemetry.
Serves a RapiDoc UI at /docs.
"""

import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, Response
from flask_cors import CORS
from flask_sock import Sock

load_dotenv()  # load .env before reading config

# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

DRONE_TYPE = os.getenv("DRONE_TYPE", "mavsdk").lower()  # "mavsdk" or "dji"
DRONE_ADDRESS = os.getenv("DRONE_ADDRESS", "udpin://0.0.0.0:14551")
DJI_SOCK_PATH = os.getenv("DJI_SOCK_PATH", "/tmp/helios-dji-telemetry.sock")
DJI_RECONNECT_S = float(os.getenv("DJI_RECONNECT_S", "2"))
TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", "30"))
WS_RATE_HZ = float(os.getenv("WS_RATE_HZ", "10"))  # WebSocket push rate
TELEM_RATE_HZ = float(os.getenv("TELEM_RATE_HZ", "2"))  # telemetry request rate

# ═══════════════════════════════════════════════════════════════
#  Shared telemetry state  (written by bg thread, read by Flask)
# ═══════════════════════════════════════════════════════════════

_lock = threading.Lock()
_state = {
    "connected": False,
    "connecting": True,
    "started_at": None,
    "position": {
        "latitude_deg": None,
        "longitude_deg": None,
        "absolute_altitude_m": None,
        "relative_altitude_m": None,
    },
    "attitude": {
        "roll_deg": None,
        "pitch_deg": None,
        "yaw_deg": None,
    },
    "battery": {
        "voltage_v": None,
        "remaining_percent": None,
    },
    "last_updated": None,
}


def _get_snapshot(*keys):
    """Return a (deep-)copy of selected top-level keys, or the full state."""
    with _lock:
        if keys:
            return {k: _state[k] for k in keys if k in _state}
        return {**_state}


def _patch(**kwargs):
    """Thread-safe update of one or more top-level keys."""
    with _lock:
        _state.update(kwargs)
        _state["last_updated"] = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
#  Backend: MAVSDK
# ═══════════════════════════════════════════════════════════════

def _start_mavsdk_backend():
    """Import mavsdk, connect, and stream telemetry (runs in daemon thread)."""
    from mavsdk import System

    async def _telemetry_loop():
        drone = System()
        await drone.connect(system_address=DRONE_ADDRESS)

        print(f"[helios] Listening on {DRONE_ADDRESS} …")
        print(f"[helios] Waiting up to {TIMEOUT}s for heartbeat")

        async def _wait_for_connection():
            async for state in drone.core.connection_state():
                if state.is_connected:
                    return

        try:
            await asyncio.wait_for(_wait_for_connection(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            _patch(connected=False, connecting=False)
            print(f"[helios] ✗ No heartbeat received after {TIMEOUT}s.")
            print("  Troubleshooting:")
            if DRONE_ADDRESS.startswith("serial"):
                print("  1. Check that the Pixhawk is powered and USB cable is connected")
                print("  2. Verify the serial device exists (ls /dev/ttyACM* /dev/ttyUSB*)")
                print("  3. Check baud rate — common values: 57600, 115200, 921600")
                print(f"  4. Current address: {DRONE_ADDRESS}")
            else:
                print("  1. In Mission Planner → Ctrl+T → add UDP output to 127.0.0.1:14551")
                print("  2. Or try a different port (14550, 14540)")
                print("  3. Make sure your drone/SITL is connected to Mission Planner first")
            return

        _patch(connected=True, connecting=False, started_at=datetime.now(timezone.utc).isoformat())
        print("[helios] ✓ Connected — requesting telemetry rates")

        tel = drone.telemetry
        try:
            await tel.set_rate_position(TELEM_RATE_HZ)
            await tel.set_rate_battery(TELEM_RATE_HZ)
            await tel.set_rate_attitude_euler(TELEM_RATE_HZ)
            await tel.set_rate_velocity_ned(TELEM_RATE_HZ)
            await tel.set_rate_gps_info(TELEM_RATE_HZ)
            await tel.set_rate_home(1)
            await tel.set_rate_in_air(1)
            await tel.set_rate_landed_state(1)
            print(f"[helios] ✓ Telemetry rates set to {TELEM_RATE_HZ} Hz")
        except Exception as e:
            print(f"[helios] ⚠ Could not set telemetry rates: {e}")
            print("[helios]   (this is fine for SITL, but real hardware may not stream data)")

        await asyncio.sleep(2)
        print("[helios] ✓ Streaming telemetry")

        async def _stream_position():
            async for pos in drone.telemetry.position():
                _patch(position={
                    "latitude_deg": round(pos.latitude_deg, 7),
                    "longitude_deg": round(pos.longitude_deg, 7),
                    "absolute_altitude_m": round(pos.absolute_altitude_m, 2),
                    "relative_altitude_m": round(pos.relative_altitude_m, 2),
                })

        async def _stream_attitude():
            async for att in drone.telemetry.attitude_euler():
                _patch(attitude={
                    "roll_deg": round(att.roll_deg, 2),
                    "pitch_deg": round(att.pitch_deg, 2),
                    "yaw_deg": round(att.yaw_deg, 2),
                })

        async def _stream_battery():
            async for bat in drone.telemetry.battery():
                _patch(battery={
                    "voltage_v": round(bat.voltage_v, 2),
                    "remaining_percent": round(bat.remaining_percent, 4),
                })

        await asyncio.gather(
            _stream_position(),
            _stream_attitude(),
            _stream_battery(),
        )

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_telemetry_loop())

    t = threading.Thread(target=_run, daemon=True, name="helios-mavsdk")
    t.start()


# ═══════════════════════════════════════════════════════════════
#  Backend: DJI (Unix Domain Socket client)
# ═══════════════════════════════════════════════════════════════

def _start_dji_backend():
    """Connect to telemetry-monitor Unix socket and read JSON lines."""

    def _apply_frame(data: dict):
        updates = {}
        pos = data.get("position")
        if pos:
            updates["position"] = {
                "latitude_deg": pos.get("latitude_deg"),
                "longitude_deg": pos.get("longitude_deg"),
                "absolute_altitude_m": pos.get("absolute_altitude_m"),
                "relative_altitude_m": pos.get("relative_altitude_m"),
            }
        att = data.get("attitude")
        if att:
            updates["attitude"] = {
                "roll_deg": att.get("roll_deg"),
                "pitch_deg": att.get("pitch_deg"),
                "yaw_deg": att.get("yaw_deg"),
            }
        bat = data.get("battery")
        if bat:
            updates["battery"] = {
                "voltage_v": bat.get("voltage_v"),
                "remaining_percent": bat.get("remaining_percent"),
            }
        if updates:
            _patch(**updates)

    def _run():
        buf = b""
        while True:
            try:
                _patch(connecting=True, connected=False)
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(DJI_SOCK_PATH)
                s.settimeout(5.0)
                _patch(connecting=False, connected=True,
                       started_at=datetime.now(timezone.utc).isoformat())
                print(f"[dji] Connected to {DJI_SOCK_PATH}")
                buf = b""

                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise ConnectionError("socket closed")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        _apply_frame(data)

            except (ConnectionError, OSError, socket.timeout) as exc:
                print(f"[dji] Connection lost ({exc}), reconnecting in {DJI_RECONNECT_S}s...")
                _patch(connecting=False, connected=False)
                try:
                    s.close()
                except Exception:
                    pass
                time.sleep(DJI_RECONNECT_S)

    t = threading.Thread(target=_run, daemon=True, name="helios-dji")
    t.start()


# ═══════════════════════════════════════════════════════════════
#  Start the selected backend
# ═══════════════════════════════════════════════════════════════

def _start_telemetry_thread():
    if DRONE_TYPE == "dji":
        print(f"[helios] Backend: DJI — reading from {DJI_SOCK_PATH}")
        _start_dji_backend()
    else:
        print(f"[helios] Backend: MAVSDK — connecting to {DRONE_ADDRESS}")
        _start_mavsdk_backend()


# ═══════════════════════════════════════════════════════════════
#  Flask application
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app)
sock = Sock(app)


# ── Documentation UI ────────────────────────────────────────
@app.route("/docs")
def docs():
    return render_template("docs.html")


# ── OpenAPI spec ────────────────────────────────────────────
@app.route("/openapi.json")
def openapi_spec():
    spec = _build_openapi_spec()
    return Response(
        json.dumps(spec, indent=2),
        mimetype="application/json",
    )


# ── REST: full telemetry snapshot ───────────────────────────
@app.route("/api/telemetry")
def api_telemetry():
    snap = _get_snapshot("position", "attitude", "battery", "last_updated")
    return jsonify(snap)


# ── REST: position only ────────────────────────────────────
@app.route("/api/telemetry/position")
def api_position():
    return jsonify(_get_snapshot("position"))


# ── REST: attitude only ────────────────────────────────────
@app.route("/api/telemetry/attitude")
def api_attitude():
    return jsonify(_get_snapshot("attitude"))


# ── REST: battery only ─────────────────────────────────────
@app.route("/api/telemetry/battery")
def api_battery():
    return jsonify(_get_snapshot("battery"))


# ── REST: connection status ─────────────────────────────────
@app.route("/api/status")
def api_status():
    snap = _get_snapshot("connected", "connecting", "started_at", "last_updated")
    snap["backend"] = DRONE_TYPE
    snap["drone_address"] = DRONE_ADDRESS if DRONE_TYPE == "mavsdk" else DJI_SOCK_PATH
    snap["ws_rate_hz"] = WS_RATE_HZ
    return jsonify(snap)


# ── WebSocket: live telemetry stream ────────────────────────
@sock.route("/ws/telemetry")
def ws_telemetry(ws):
    """
    Push the latest telemetry snapshot to the client at WS_RATE_HZ.
    The client may send a JSON message to filter fields:
        {"subscribe": ["position"]}           — only position
        {"subscribe": ["attitude", "battery"]} — attitude + battery
        {"subscribe": ["all"]}                 — everything (default)
    """
    interval = 1.0 / WS_RATE_HZ
    fields = None  # None = send everything

    while True:
        # ── Check for incoming filter messages (non-blocking) ──
        try:
            msg = ws.receive(timeout=0)
            if msg:
                try:
                    payload = json.loads(msg)
                    subs = payload.get("subscribe", ["all"])
                    if "all" in subs:
                        fields = None
                    else:
                        fields = [s for s in subs if s in ("position", "attitude", "battery")]
                except (json.JSONDecodeError, AttributeError):
                    pass
        except Exception:
            pass

        # ── Build & send snapshot ──────────────────────────────
        if fields:
            snap = _get_snapshot(*fields, "last_updated")
        else:
            snap = _get_snapshot("position", "attitude", "battery", "last_updated")

        try:
            ws.send(json.dumps(snap))
        except Exception:
            break  # client disconnected

        time.sleep(interval)


# ═══════════════════════════════════════════════════════════════
#  OpenAPI 3.0 specification (dict-based)
# ═══════════════════════════════════════════════════════════════

def _build_openapi_spec():
    position_schema = {
        "type": "object",
        "properties": {
            "latitude_deg":  {"type": "number", "example": 34.0522017},
            "longitude_deg": {"type": "number", "example": -118.2436842},
            "absolute_altitude_m": {"type": "number", "example": 125.43},
            "relative_altitude_m": {"type": "number", "example": 42.10},
        },
    }
    attitude_schema = {
        "type": "object",
        "properties": {
            "roll_deg":  {"type": "number", "example": 1.23},
            "pitch_deg": {"type": "number", "example": -0.45},
            "yaw_deg":   {"type": "number", "example": 178.90},
        },
    }
    battery_schema = {
        "type": "object",
        "properties": {
            "voltage_v":          {"type": "number", "example": 12.4},
            "remaining_percent":  {"type": "number", "example": 0.87},
        },
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Helios SBC Telemetry API",
            "version": "1.0.0",
            "description": (
                "Real-time drone telemetry service for the Helios eVTOL fleet.\n\n"
                "## REST Endpoints\n"
                "Standard JSON endpoints for on-demand telemetry snapshots.\n\n"
                "## WebSocket Stream\n"
                "Connect to `ws://<host>/ws/telemetry` for a continuous stream "
                "of telemetry data pushed at a configurable rate.\n\n"
                "### Subscribing to specific fields\n"
                "After connecting, send a JSON message to filter the data:\n"
                "```json\n"
                '{"subscribe": ["position"]}\n'
                "```\n"
                "Valid field names: `position`, `attitude`, `battery`, `all` (default)."
            ),
            "contact": {"name": "Helios Aerospace", "url": "https://helios.aero"},
        },
        "servers": [
            {"url": "/", "description": "Current host"},
        ],
        "tags": [
            {"name": "Telemetry", "description": "Real-time drone sensor data"},
            {"name": "Status",    "description": "Service & connection health"},
        ],
        "paths": {
            "/api/telemetry": {
                "get": {
                    "tags": ["Telemetry"],
                    "summary": "Full telemetry snapshot",
                    "description": "Returns the latest position, attitude, and battery readings.",
                    "operationId": "getTelemetry",
                    "responses": {
                        "200": {
                            "description": "Telemetry data",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "position": position_schema,
                                            "attitude": attitude_schema,
                                            "battery":  battery_schema,
                                            "last_updated": {"type": "string", "format": "date-time"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "/api/telemetry/position": {
                "get": {
                    "tags": ["Telemetry"],
                    "summary": "Position data",
                    "description": "Latest GPS position (lat, lon, altitude).",
                    "operationId": "getPosition",
                    "responses": {
                        "200": {
                            "description": "Position data",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"position": position_schema},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "/api/telemetry/attitude": {
                "get": {
                    "tags": ["Telemetry"],
                    "summary": "Attitude data",
                    "description": "Latest roll, pitch, and yaw (degrees).",
                    "operationId": "getAttitude",
                    "responses": {
                        "200": {
                            "description": "Attitude data",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"attitude": attitude_schema},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "/api/telemetry/battery": {
                "get": {
                    "tags": ["Telemetry"],
                    "summary": "Battery data",
                    "description": "Latest battery voltage and remaining charge.",
                    "operationId": "getBattery",
                    "responses": {
                        "200": {
                            "description": "Battery data",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"battery": battery_schema},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "/api/status": {
                "get": {
                    "tags": ["Status"],
                    "summary": "Service status",
                    "description": "Connection state, uptime, and configuration.",
                    "operationId": "getStatus",
                    "responses": {
                        "200": {
                            "description": "Status information",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "connected":     {"type": "boolean"},
                                            "connecting":    {"type": "boolean"},
                                            "started_at":    {"type": "string", "format": "date-time", "nullable": True},
                                            "last_updated":  {"type": "string", "format": "date-time", "nullable": True},
                                            "drone_address": {"type": "string", "example": "udpin://0.0.0.0:14551"},
                                            "ws_rate_hz":    {"type": "number", "example": 10},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


# ═══════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _start_telemetry_thread()

    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))

    print(f"[helios] API server starting on http://{host}:{port}")
    print(f"[helios] Docs UI → http://{host}:{port}/docs")
    print(f"[helios] WebSocket → ws://{host}:{port}/ws/telemetry\n")

    app.run(host=host, port=port, debug=False)
