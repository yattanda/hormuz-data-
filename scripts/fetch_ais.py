"""
fetch_ais.py
AISstream.io WebSocket から ホルムズ海峡バウンディングボックス内の
船舶データを取得し、data/ais-snapshot.json に保存する。

必要環境変数:
  AISSTREAM_API_KEY : AISstream.io の APIキー (GitHub Secrets に設定)
"""

import asyncio
import websockets
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ホルムズ海峡 + ペルシャ湾南部 バウンディングボックス
BBOX = {
    "min_lat": 22.0,
    "max_lat": 27.0,
    "min_lon": 55.5,
    "max_lon": 60.5,
}

# 収集時間（秒）
COLLECT_SECONDS = 180

# ダーク船補正係数
DARK_FACTOR = 1.35

def classify_ship_type(type_code: int) -> str:
    if 80 <= type_code <= 89:
        return "tanker"
    elif 70 <= type_code <= 79:
        return "cargo"
    else:
        return "other"

async def collect_ais_data(api_key: str) -> dict:
    url = "wss://stream.aisstream.io/v0/stream"

    subscribe_message = {
        "APIKey": api_key,
        "BoundingBoxes": [[
            [BBOX["min_lat"], BBOX["min_lon"]],
            [BBOX["max_lat"], BBOX["max_lon"]]
        ]],
        "FilterMessageTypes": ["PositionReport"]
    }

    vessel_set = set()
    breakdown = defaultdict(set)

    print(f"[AIS] Connecting to {url} ...")
    print(f"[AIS] Collecting for {COLLECT_SECONDS} seconds...")

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            await ws.send(json.dumps(subscribe_message))
            print("[AIS] Subscribed. Receiving data...")

            deadline = asyncio.get_event_loop().time() + COLLECT_SECONDS
            msg_count = 0

            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    msg = json.loads(raw)
                    msg_count += 1

                    meta = msg.get("MetaData", {})
                    mmsi = meta.get("MMSI")
                    if not mmsi:
                        continue

                    msg_type = msg.get("Message", {})
                    pos_report = msg_type.get("PositionReport", {})
                    ship_type = pos_report.get("ShipType", 0)

                    cat = classify_ship_type(ship_type)
                    vessel_set.add(mmsi)
                    breakdown[cat].add(mmsi)

                    if msg_count % 50 == 0:
                        print(f"[AIS] {msg_count} messages, {len(vessel_set)} unique vessels...")

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("[AIS] Connection closed.")
                    break

    except Exception as e:
        print(f"[AIS] Connection error: {e}", file=sys.stderr)
        raise

    print(f"[AIS] Done: {len(vessel_set)} unique vessels detected")
    return {
        "vessel_set": vessel_set,
        "breakdown": {k: len(v) for k, v in breakdown.items()},
        "msg_count": msg_count,
    }

def build_snapshot(data: dict) -> dict:
    vessels = len(data["vessel_set"])
    breakdown = data["breakdown"]
    tanker = breakdown.get("tanker", 0)
    cargo  = breakdown.get("cargo", 0)
    other  = breakdown.get("other", 0)
    estimated = int(round(vessels * DARK_FACTOR))

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")

    return {
        "updated_at": now,
        "source": "AISstream.io",
        "collection_duration_sec": COLLECT_SECONDS,
        "bbox": BBOX,
        "vessels_detected": vessels,
        "breakdown": {
            "tanker": tanker,
            "cargo": cargo,
            "other": other,
        },
        "dark_estimate_factor": DARK_FACTOR,
        "estimated_actual": estimated,
        "messages_received": data["msg_count"],
        "note": (
            "AIS非検出（ダークシッピング）船舶を含む推定値。"
            f"補正係数{DARK_FACTOR}はUANI/Kpler公開レポート推定ベース。"
            "陸上受信局の地理的限界あり（外洋中央部はカバレッジ不完全）。"
        )
    }

def save_snapshot(snapshot: dict, path: str = "data/ais-snapshot.json"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[AIS] Saved to {path}")

async def main():
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        print("[AIS] ERROR: AISSTREAM_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    data = await collect_ais_data(api_key)
    snapshot = build_snapshot(data)
    save_snapshot(snapshot)
    print(f"[AIS] Vessels: {snapshot['vessels_detected']}, Estimated: {snapshot['estimated_actual']}")

if __name__ == "__main__":
    asyncio.run(main())
