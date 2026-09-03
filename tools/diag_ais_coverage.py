"""
diag_ais_coverage.py - AISstream.io の受信局カバレッジ地理分布 診断ツール
（読み取り専用・ファイル書き込みなし）

diag_ais.py で「ホルムズbbox=0件 / 全世界=受信あり」と判明した後の切り分け用。
全世界bboxで受信し、届いた船の位置を地理的に集計して、
中東・ペルシャ湾周辺にAISstreamのカバレッジが存在するのかを判定する。

使い方（PowerShell）:
    $env:AISSTREAM_API_KEY = "xxxxx"
    python diag_ais_coverage.py

    python diag_ais_coverage.py --seconds 300     収集時間を変える（既定180秒）

終了コード:
    0  ホルムズ海峡そのもので受信あり -> bboxが狭い等、こちら側の問題
    10 中東圏では受信あり             -> カバレッジは存在。bbox拡張で救える可能性あり
    20 中東圏が完全に空白             -> AISstreamに当該地域の受信局が無い（打つ手なし）
    30 接続・認証エラー
"""

import argparse
import asyncio
import json
import math
import os
import ssl
import sys
from collections import Counter
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("websockets が未インストールです。次を実行してください:\n    python -m pip install websockets")
    sys.exit(30)

URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BBOX = [[-90.0, -180.0], [90.0, 180.0]]

# ホルムズ海峡の中心（最狭部付近）
HORMUZ_CENTER = (26.5667, 56.25)

# 判定に使う関心領域。(名前, min_lat, max_lat, min_lon, max_lon)
REGIONS = [
    ("ホルムズ海峡 bbox（本番と同一）", 22.0, 27.0, 55.5, 60.5),
    ("ペルシャ湾 全域",                 23.0, 31.0, 47.0, 57.0),
    ("オマーン湾・アラビア海",          10.0, 27.0, 55.0, 70.0),
    ("紅海・アデン湾",                  10.0, 30.0, 32.0, 46.0),
    ("東地中海",                        30.0, 38.0, 25.0, 37.0),
    ("インド西岸",                       5.0, 25.0, 65.0, 80.0),
    ("マラッカ海峡・シンガポール",      -2.0,  8.0, 95.0, 108.0),
]

# 中東圏の広域判定（これが0なら地域丸ごと空白）
MIDDLE_EAST = (5.0, 40.0, 30.0, 75.0)   # min_lat, max_lat, min_lon, max_lon


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print("[{}] {}".format(ts(), msg), flush=True)


def in_box(lat, lon, box):
    min_lat, max_lat, min_lon, max_lon = box
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def haversine_km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def cell_label(lat, lon):
    """10度メッシュのラベルを返す（カバレッジの粗い分布把握用）。"""
    la = int(math.floor(lat / 10.0) * 10)
    lo = int(math.floor(lon / 10.0) * 10)
    return "lat {:+03d}..{:+03d} / lon {:+04d}..{:+04d}".format(la, la + 10, lo, lo + 10)


async def collect(api_key, seconds):
    subscribe = {
        "APIKey": api_key,
        "BoundingBoxes": [GLOBAL_BBOX],
        "FilterMessageTypes": ["PositionReport"],
    }

    vessels = {}          # mmsi -> (lat, lon, name)
    cells = Counter()
    frames = 0
    error = None

    log("全世界bboxで {} 秒間受信します（同時接続は3本まで。他のクライアントは止めてください）".format(seconds))
    ssl_ctx = ssl.create_default_context()

    try:
        async with websockets.connect(URL, ping_interval=None, ssl=ssl_ctx) as ws:
            await ws.send(json.dumps(subscribe))
            log("購読メッセージ送信完了。受信中...")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + seconds
            last_report = loop.time()

            while loop.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    now = loop.time()
                    if now - last_report >= 20:
                        log("  ...{} 隻収集 / 残り {} 秒".format(len(vessels), int(deadline - now)))
                        last_report = now
                    continue
                except ConnectionClosed as e:
                    log("!! 切断されました: code={} reason={!r}".format(
                        getattr(e, "code", "?"), getattr(e, "reason", "")))
                    break

                frames += 1
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                meta = msg.get("MetaData")
                if not isinstance(meta, dict) or not meta:
                    continue

                lat = meta.get("latitude")
                lon = meta.get("longitude")
                mmsi = meta.get("MMSI") or meta.get("MMSI_String")
                if lat is None or lon is None or mmsi is None:
                    continue
                # AISの「位置不明」既定値は除外
                if abs(lat) > 90 or abs(lon) > 180 or (lat == 91.0 or lon == 181.0):
                    continue

                name = (meta.get("ShipName") or "").strip()
                vessels[str(mmsi)] = (float(lat), float(lon), name)
                cells[cell_label(float(lat), float(lon))] += 1

    except Exception as e:
        error = "{}: {}".format(type(e).__name__, e)
        log("!! " + error)

    return vessels, cells, frames, error


def report(vessels, cells):
    print()
    print("=" * 68)
    print("受信結果: ユニーク {} 隻".format(len(vessels)))
    print("=" * 68)

    print()
    print("--- 関心領域ごとの受信隻数 ---")
    region_counts = {}
    for name, mnla, mxla, mnlo, mxlo in REGIONS:
        box = (mnla, mxla, mnlo, mxlo)
        n = sum(1 for (la, lo, _) in vessels.values() if in_box(la, lo, box))
        region_counts[name] = n
        mark = "  " if n else "<-- ゼロ"
        print("  {:<30} {:>6} 隻 {}".format(name, n, mark))

    me_n = sum(1 for (la, lo, _) in vessels.values() if in_box(la, lo, MIDDLE_EAST))
    print()
    print("  中東広域（緯度5-40N / 経度30-75E）: {} 隻".format(me_n))

    print()
    print("--- 受信が集中している10度メッシュ 上位15 ---")
    for label, n in cells.most_common(15):
        print("  {:<40} {:>6} 件".format(label, n))

    print()
    print("--- ホルムズ海峡中心({:.4f}N, {:.4f}E)に最も近い受信船 上位10 ---".format(*HORMUZ_CENTER))
    ranked = sorted(
        ((haversine_km(HORMUZ_CENTER, (la, lo)), mmsi, la, lo, nm)
         for mmsi, (la, lo, nm) in vessels.items())
    )
    for dist, mmsi, la, lo, nm in ranked[:10]:
        print("  {:>8.0f} km  MMSI {:<12} {:>8.3f}N {:>9.3f}E  {}".format(
            dist, mmsi, la, lo, nm[:24]))

    return region_counts, me_n, (ranked[0][0] if ranked else None)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("AISSTREAM_API_KEY"))
    ap.add_argument("--seconds", type=int, default=180)
    args = ap.parse_args()

    if not args.key:
        print("AISSTREAM_API_KEY が未設定です。環境変数か --key で指定してください。")
        return 30

    log("カバレッジ診断開始  websockets={}  python={}".format(
        getattr(websockets, "__version__", "?"), sys.version.split()[0]))

    vessels, cells, frames, error = await collect(args.key, args.seconds)

    if error and not vessels:
        print("接続・受信に失敗しました: " + error)
        return 30
    if not vessels:
        print("1隻も受信できませんでした。diag_ais.py から状況が変わっています。")
        return 30

    region_counts, me_n, nearest_km = report(vessels, cells)

    print()
    print("=" * 68)
    print("判定")
    print("=" * 68)

    hormuz_n = region_counts["ホルムズ海峡 bbox（本番と同一）"]
    if hormuz_n > 0:
        print("ホルムズbbox内で {} 隻を受信しました。".format(hormuz_n))
        print("-> カバレッジはあります。専用購読だけ0件になる理由を別途調べる必要があります。")
        return 0

    if me_n > 0:
        print("ホルムズbboxは0隻ですが、中東広域では {} 隻を受信しました。".format(me_n))
        print("   ホルムズ中心からの最短距離: {:.0f} km".format(nearest_km))
        print("-> 受信局は近隣に存在します。bboxの拡張で実データを拾える可能性があります。")
        return 10

    print("中東広域（緯度5-40N / 経度30-75E）で受信が完全にゼロでした。")
    print("   ホルムズ中心から最も近い受信船でも {:.0f} km 離れています。".format(nearest_km))
    print("-> AISstream には当該地域の受信局が存在しません。")
    print("   bboxをどう調整しても実データは取得できません。代替ソースへの移行が必要です。")
    return 20


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n中断しました。")
        sys.exit(130)
