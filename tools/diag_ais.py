"""
diag_ais.py - AISstream.io 疎通診断ツール（読み取り専用・ファイル書き込みなし）

本番の fetch_ais.py とは独立した診断用スクリプト。
握りつぶされている情報（生フレーム・エラー応答・切断理由）を全て表示する。

使い方（PowerShell）:
    $env:AISSTREAM_API_KEY = "xxxxx"
    python diag_ais.py

    python diag_ais.py --seconds 120        収集時間を変える（既定60秒）
    python diag_ais.py --key xxxxx          環境変数の代わりに直接指定
    python diag_ais.py --skip-global        全世界bboxの対照実験を省略

終了コード:
    0  ホルムズbboxでデータ受信あり   -> AISstream は正常。原因は本番コード側
    10 ホルムズ0件 / 全世界は受信あり -> bbox・受信局カバレッジの問題
    20 両方0件                        -> サービス側またはアカウント側の問題
    30 接続・認証エラー               -> 詳細はログ参照
"""

import argparse
import asyncio
import json
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
    print("websockets が未インストールです。次を実行してください:\n    pip install websockets")
    sys.exit(30)

# websockets のバージョンによって例外クラス名が違うため、無ければダミーを使う
try:
    from websockets.exceptions import InvalidStatus as _UpgradeRejected
except ImportError:
    try:
        from websockets.exceptions import InvalidStatusCode as _UpgradeRejected
    except ImportError:
        class _UpgradeRejected(Exception):
            pass

URL = "wss://stream.aisstream.io/v0/stream"

HORMUZ_BBOX = [[22.0, 55.5], [27.0, 60.5]]   # 本番 fetch_ais.py と同一
GLOBAL_BBOX = [[-90.0, -180.0], [90.0, 180.0]]

RAW_FRAMES_TO_DUMP = 5      # 生のまま全文表示するフレーム数
MAX_FRAME_CHARS = 1200      # 1フレームあたりの表示上限


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print("[{}] {}".format(ts(), msg), flush=True)


async def run_phase(label, api_key, bbox, seconds):
    """1フェーズ分の接続・購読・受信を行い、結果を辞書で返す。"""
    result = {
        "label": label,
        "connected": False,
        "frames": 0,
        "position_reports": 0,
        "unique_mmsi": set(),
        "message_types": Counter(),
        "non_ais_frames": [],   # MetaData を持たない = エラー応答等
        "close_reason": None,
        "error": None,
    }

    subscribe = {
        "APIKey": api_key,
        "BoundingBoxes": [bbox],
        "FilterMessageTypes": ["PositionReport"],
    }

    # 購読メッセージをキーだけ伏せて表示（形式の目視確認用）
    masked = dict(subscribe)
    masked["APIKey"] = "{}...{} (len={})".format(api_key[:4], api_key[-4:], len(api_key))
    log("=== PHASE: {} ===".format(label))
    log("送信する購読メッセージ: " + json.dumps(masked, ensure_ascii=False))

    ssl_ctx = ssl.create_default_context()

    try:
        async with websockets.connect(
            URL, ping_interval=20, ping_timeout=10, ssl=ssl_ctx
        ) as ws:
            result["connected"] = True
            log("WebSocket 接続成功（TLSハンドシェイク・HTTPアップグレード完了）")

            await ws.send(json.dumps(subscribe))
            log("購読メッセージ送信完了。{} 秒間受信します...".format(seconds))

            loop = asyncio.get_event_loop()
            deadline = loop.time() + seconds
            last_report = loop.time()

            while loop.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    now = loop.time()
                    if now - last_report >= 15:
                        log("  ...受信 {} フレーム / 残り {} 秒".format(
                            result["frames"], int(deadline - now)))
                        last_report = now
                    continue
                except ConnectionClosed as e:
                    result["close_reason"] = "code={} reason={!r}".format(
                        getattr(e, "code", "?"), getattr(e, "reason", ""))
                    log("!! サーバから切断されました: " + result["close_reason"])
                    break

                result["frames"] += 1

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                # 最初の数フレームは加工せず全文表示する
                if result["frames"] <= RAW_FRAMES_TO_DUMP:
                    body = raw if len(raw) <= MAX_FRAME_CHARS else raw[:MAX_FRAME_CHARS] + " ...(truncated)"
                    log("--- 生フレーム #{} ---\n{}\n---".format(result["frames"], body))

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # JSON でない応答（プレーンテキストのエラー等）
                    result["non_ais_frames"].append(raw[:400])
                    log("!! JSONでない応答: " + repr(raw[:400]))
                    continue

                if not isinstance(msg, dict):
                    result["non_ais_frames"].append(raw[:400])
                    continue

                meta = msg.get("MetaData")
                if not isinstance(meta, dict) or not meta:
                    # ここが本番 fetch_ais.py で黙って捨てられている領域
                    result["non_ais_frames"].append(raw[:400])
                    log("!! AISデータではない応答（本番コードが黙って捨てている種類）: " + raw[:400])
                    continue

                mtype = msg.get("MessageType", "?")
                result["message_types"][mtype] += 1
                if mtype == "PositionReport":
                    result["position_reports"] += 1

                mmsi = meta.get("MMSI") or meta.get("MMSI_String")
                if mmsi:
                    result["unique_mmsi"].add(str(mmsi))

    except _UpgradeRejected as e:
        result["error"] = "HTTPアップグレード拒否: {}".format(e)
        log("!! " + result["error"] + "  （429ならレート制限、401/403なら認証）")
    except ssl.SSLCertVerificationError as e:
        result["error"] = "TLS証明書検証エラー: {}".format(e)
        log("!! " + result["error"])
    except OSError as e:
        result["error"] = "ネットワークエラー: {}".format(e)
        log("!! " + result["error"])
    except Exception as e:
        result["error"] = "{}: {}".format(type(e).__name__, e)
        log("!! 予期しない例外: " + result["error"])

    log("PHASE {} 終了: フレーム {} / PositionReport {} / ユニークMMSI {} / 非AIS応答 {}".format(
        label, result["frames"], result["position_reports"],
        len(result["unique_mmsi"]), len(result["non_ais_frames"])))
    if result["message_types"]:
        log("  受信メッセージ種別: " + str(dict(result["message_types"])))
    print(flush=True)
    return result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("AISSTREAM_API_KEY"))
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--skip-global", action="store_true")
    args = ap.parse_args()

    if not args.key:
        print("AISSTREAM_API_KEY が未設定です。環境変数か --key で指定してください。")
        return 30

    log("診断開始  endpoint={}  websockets={}  python={}".format(
        URL, getattr(websockets, "__version__", "?"), sys.version.split()[0]))
    print(flush=True)

    hormuz = await run_phase("ホルムズ海峡 bbox (本番と同一)", args.key, HORMUZ_BBOX, args.seconds)

    glob = None
    if not args.skip_global and hormuz["position_reports"] == 0 and hormuz["error"] is None:
        log("ホルムズbboxで0件のため、全世界bboxで再試行します（同時接続上限を避けるため5秒待機）")
        await asyncio.sleep(5)
        glob = await run_phase("全世界 bbox (対照実験)", args.key, GLOBAL_BBOX, args.seconds)

    # ---- 判定 ----
    print("=" * 60)
    print("判定")
    print("=" * 60)

    if hormuz["error"]:
        print("接続・認証の段階で失敗しています: " + hormuz["error"])
        print("-> APIキーの有効性、またはネットワーク/レート制限を確認してください。")
        return 30

    if hormuz["position_reports"] > 0:
        print("ホルムズ海峡bboxで {} 隻を受信しました。".format(len(hormuz["unique_mmsi"])))
        print("-> AISstream は正常に動作しています。原因は本番の fetch_ais.py 側です。")
        return 0

    if glob and glob["position_reports"] > 0:
        print("ホルムズbboxは0件、全世界bboxは {} 隻を受信。".format(len(glob["unique_mmsi"])))
        print("-> サービスは生きています。bbox の指定か、当該海域の受信局カバレッジの問題です。")
        return 10

    print("ホルムズbbox・全世界bboxとも0件でした（接続は維持されている）。")
    print("-> AISstream のサービス側、またはアカウント/キーの有効化状態の問題です。")
    print("   https://aisstream.io/apikeys でキーの状態と、アカウントが activated かを確認してください。")
    if hormuz["non_ais_frames"] or (glob and glob["non_ais_frames"]):
        print("   ※ 上のログの「AISデータではない応答」にサーバ側のメッセージが出ています。")
    else:
        print("   ※ サーバは何のエラーも返さず沈黙しています（本家 issue #15/#210/#282 と同一症状）。")
    return 20


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n中断しました。")
        sys.exit(130)
