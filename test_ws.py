"""
Helios SBC Telemetry — WebSocket Test Client
──────────────────────────────────────────────
Connect to the live telemetry WebSocket and print frames to the console.

Usage:
    python test_ws.py                        # all telemetry, default host
    python test_ws.py --fields position      # position only
    python test_ws.py --fields attitude battery
    python test_ws.py --host 192.168.1.50 --port 5000

Requires: websocket-client  (pip install websocket-client)
"""

import argparse
import json
import sys
import time

try:
    from websocket import create_connection, WebSocketException
except ImportError:
    print("Missing dependency.  Install it with:\n  pip install websocket-client")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Helios telemetry WebSocket test client")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", default=5000, type=int, help="Server port (default: 5000)")
    parser.add_argument(
        "--fields",
        nargs="+",
        choices=["all", "position", "attitude", "battery"],
        default=["all"],
        help="Telemetry fields to subscribe to (default: all)",
    )
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}/ws/telemetry"
    print(f"Connecting to {url} …")

    try:
        ws = create_connection(url)
    except Exception as exc:
        print(f"✗ Could not connect: {exc}")
        sys.exit(1)

    print("✓ Connected\n")

    # Send subscription filter
    subscribe_msg = json.dumps({"subscribe": args.fields})
    ws.send(subscribe_msg)
    print(f"  Subscribed to: {', '.join(args.fields)}\n")
    print("-" * 60)

    try:
        count = 0
        while True:
            raw = ws.recv()
            data = json.loads(raw)
            count += 1

            ts = data.get("last_updated", "—")
            parts = []

            if "position" in data:
                p = data["position"]
                parts.append(
                    f"  POS  lat={p['latitude_deg']}  lon={p['longitude_deg']}  "
                    f"alt={p['relative_altitude_m']}m"
                )

            if "attitude" in data:
                a = data["attitude"]
                parts.append(
                    f"  ATT  roll={a['roll_deg']}°  pitch={a['pitch_deg']}°  "
                    f"yaw={a['yaw_deg']}°"
                )

            if "battery" in data:
                b = data["battery"]
                pct = f"{b['remaining_percent'] * 100:.1f}%" if b["remaining_percent"] is not None else "—"
                volts = f"{b['voltage_v']}V" if b["voltage_v"] is not None else "—"
                parts.append(f"  BAT  {pct}  {volts}")

            header = f"[#{count}  {ts}]"
            print(header)
            for line in parts:
                print(line)
            print()

    except KeyboardInterrupt:
        print(f"\nReceived {count} frames.  Bye!")
    except WebSocketException as exc:
        print(f"\nWebSocket error: {exc}")
    finally:
        ws.close()


if __name__ == "__main__":
    main()
