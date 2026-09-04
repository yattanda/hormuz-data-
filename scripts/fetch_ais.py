"""
fetch_ais.py
VesselAPI (vesselapi.com) の REST API から ホルムズ海峡 bbox 内の
AIS 位置報告を取得し、data/ais-snapshot.json に保存する。

このファイルは **実測値の唯一の書き手** である。
Gemini による推計は data/ais-estimate.json（scripts/fetch_manual.py）が担当し、
両者は決して同じファイルに書かない。
（2026-04〜09 に、両者が data/ais-snapshot.json を交互に上書きする事故が起きている）

必要環境変数:
  VESSELAPI_API_KEY : VesselAPI の APIキー (GitHub Secrets に設定)

終了コード:
  0  取得成功（0隻でも、健全性を確認できていれば成功）
  2  認証・権限エラー（401/403）。キーの失効が疑われる
  3  リクエストが不正（400）。bbox スパン制約などを確認する
  4  接続エラー・想定外のレスポンス

  ※ 0 以外を返した場合はファイルを書かない。前回値がそのまま残り、
     表示側の鮮度チェック（48時間）が推計へのフォールバックを判断する。
     「黙って 0 を書き続ける」ことが 4.5 か月ぶんの誤データを生んだため、
     失敗は必ずワークフローの失敗として表面化させる。

運用上の注意:
  VesselAPI の API キーは最長 90 日で失効する。
  失効すると 401 になり本スクリプトは終了コード 2 で失敗する（＝通知が飛ぶ）。
  90 日ごとにキーを再発行し、GitHub Secret を更新すること。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://api.vesselapi.com/v1/location/vessels/bounding-box"

# ホルムズ海峡の本体と主要通航路。
# VesselAPI は bbox の総スパン |dLat| + |dLon| を 4 度以内に制限する
# （2026-09-03 に実測で判明。公開ドキュメントに記載なし）。
# ここは 1.2 + 1.4 = 2.6 度で制約内。
# 旧 AISstream 版の BBOX（lat 22-27 / lon 55.5-60.5）はスパン和 10 度で通らない。
BBOX = {
    "lat_bottom": 25.8,
    "lat_top": 27.0,
    "lon_left": 55.6,
    "lon_right": 57.0,
}

# ホルムズが 0 隻だったときにだけ叩く対照海域。
# 世界有数の輻輳海域であり、AIS が機能していれば必ず船が返る。
# 「ホルムズに船がいない」のか「API が全体として空を返している」のかを切り分ける。
CONTROL_BBOX = {
    "label": "シンガポール海峡",
    "lat_bottom": 1.0,
    "lat_top": 1.4,
    "lon_left": 103.4,
    "lon_right": 104.0,
}

# 対地速力がこれ未満の船は錨泊・漂泊とみなす。
# bbox 南西端にホル・ファッカン沖の錨泊地が入るため、切り分けないと
# 錨泊船を通航量として数えてしまう。
MOVING_KT = 1.0

# 1ページあたりの取得件数（API 上限）。
# 返るのは「船のリスト」ではなく「位置報告のリスト」であり、
# limit=50 は 50 隻ではなく 50 メッセージを意味する。MMSI で重複排除が必須。
PAGE_LIMIT = 50

# ページングの安全弁。実測では 3 ページで完了する（2026-09-03）。
# 無料枠 150 コール/月 を暴走で食い潰さないための上限。
MAX_PAGES = 10

TIMEOUT = 30
RETRIES = 2


class FetchError(Exception):
    """終了コードを持つ取得エラー。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _call(key, box, token=None):
    """bounding-box を1回叩く。(status, body) を返す。status None は接続失敗。"""
    params = {
        "filter.lonLeft": box["lon_left"],
        "filter.lonRight": box["lon_right"],
        "filter.latBottom": box["lat_bottom"],
        "filter.latTop": box["lat_top"],
        "pagination.limit": PAGE_LIMIT,
    }
    if token:
        params["pagination.nextToken"] = token

    # time.from / time.to は指定しない（API 既定の窓を使う）。
    # 4時間窓に広げるとコールが2.3倍になる一方、移動中の船は5→6隻しか増えず、
    # 増分はすべて錨泊船だった（2026-09-03 実測）。既定窓で足りる。
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            raw = res.read().decode("utf-8", "replace")
            status = res.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        print("[AIS] 接続エラー: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return None, None

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        print("[AIS] JSON として解釈できない応答: {}".format(raw[:300]), file=sys.stderr)
        return status, None


def call_with_retry(key, box, token=None):
    """接続エラーのみリトライする。HTTP エラーはそのまま返す（クォータを無駄にしない）。"""
    for attempt in range(RETRIES + 1):
        status, body = _call(key, box, token=token)
        if status is not None:
            return status, body
        if attempt < RETRIES:
            wait = (attempt + 1) * 10
            print("[AIS] {} 秒待って再試行します...".format(wait), file=sys.stderr)
            time.sleep(wait)
    return None, None


def err_code(body):
    """エラー応答から code を取り出す。"""
    if isinstance(body, dict):
        return (body.get("error") or {}).get("code", "")
    return ""


def collect(key, box, label):
    """bbox の全ページを辿り、MMSI で重複排除した船舶辞書を返す。

    戻り値: (vessels_by_mmsi, reports, pages, truncated)
    失敗時は FetchError を送出する。
    """
    seen = {}
    reports = 0
    pages = 0
    token = None

    print("[AIS] {} を取得します（lat {}-{} / lon {}-{}）".format(
        label, box["lat_bottom"], box["lat_top"], box["lon_left"], box["lon_right"]))

    while pages < MAX_PAGES:
        status, body = call_with_retry(key, box, token=token)
        pages += 1

        if status is None:
            raise FetchError(4, "接続に繰り返し失敗しました")
        if status in (401, 403):
            raise FetchError(2, "認証・権限エラー HTTP {} (code={})。"
                                "APIキーの有効期限（最長90日）を確認してください"
                                .format(status, err_code(body)))
        if status == 400:
            raise FetchError(3, "リクエストが不正です HTTP 400 (code={})。"
                                "bbox のスパン制約（|dLat|+|dLon| <= 4度）を確認してください"
                                .format(err_code(body)))
        if status != 200 or not isinstance(body, dict):
            raise FetchError(4, "想定外の応答 HTTP {} (code={})".format(status, err_code(body)))

        vessels = body.get("vessels") or []
        reports += len(vessels)
        for v in vessels:
            mmsi = v.get("mmsi")
            if mmsi is not None:
                seen[mmsi] = v

        token = body.get("nextToken")
        print("[AIS]   {} ページ目: {} 件（累計ユニーク {} 隻）".format(
            pages, len(vessels), len(seen)))

        if not token or not vessels:
            break

    truncated = bool(token)
    if truncated:
        print("[AIS] 警告: {} ページで打ち切りました。実数はこれを上回ります".format(MAX_PAGES),
              file=sys.stderr)

    return seen, reports, pages, truncated


def split_by_speed(vessels):
    """対地速力で移動中と停泊・漂泊に分ける。

    sog は船によって欠けることがある（返るフィールドは船ごとに可変）。
    欠けている場合は移動中と断定できないため停泊側に寄せる。
    """
    moving = 0
    stationary = 0
    for v in vessels.values():
        sog = v.get("sog")
        if isinstance(sog, (int, float)) and sog >= MOVING_KT:
            moving += 1
        else:
            stationary += 1
    return moving, stationary


def verify_coverage(key, vessel_count):
    """0 隻だったときだけ対照海域を叩き、API 自体が生きているかを確かめる。

    「取得成功かつ0隻」には2つの原因があり、ホルムズの応答だけでは区別できない:
      (1) 本当にホルムズに船がいない  -> 封鎖の最重要シグナル。表示すべき
      (2) API 側のカバレッジ喪失・障害 -> 誤報になる。推計へ退避すべき
    輻輳海域で船が返れば (1)、そこも 0 なら (2) と判断する。

    船が取れている通常時は追加コールを消費しない（月間コール数に影響しない）。
    """
    if vessel_count > 0:
        return {"verified": True, "detail": None}

    print("[AIS] ホルムズが 0 隻でした。対照海域で API の生存を確認します...")
    try:
        control, _, _, _ = collect(key, CONTROL_BBOX, "対照: " + CONTROL_BBOX["label"])
    except FetchError as e:
        # 対照が叩けない = API 側の問題。0 隻を実測として信用できない
        print("[AIS] 対照海域の取得に失敗: {}".format(e), file=sys.stderr)
        return {
            "verified": False,
            "detail": {
                "performed": True,
                "control_area": CONTROL_BBOX["label"],
                "control_vessels": None,
                "result": "control_fetch_failed",
                "note": "対照海域の取得に失敗したため、0 隻を実測として扱えない",
            },
        }

    n = len(control)
    ok = n > 0
    print("[AIS] 対照海域: {} 隻 -> ホルムズの 0 隻は {}".format(
        n, "実測として信用できる" if ok else "API 側の問題が疑われる"))

    return {
        "verified": ok,
        "detail": {
            "performed": True,
            "control_area": CONTROL_BBOX["label"],
            "control_vessels": n,
            "result": "control_has_vessels" if ok else "control_also_empty",
            "note": ("対照海域で船舶を取得できたため、ホルムズの 0 隻は実測値として扱える"
                     if ok else
                     "対照海域も 0 隻。API 側の障害が疑われるため 0 隻を実測として扱えない"),
        },
    }


def build_snapshot(vessels, reports, pages, truncated, coverage):
    """ais-snapshot.json の中身を組み立てる。

    このファイルには **実測値しか入れない**。
    ダーク船補正のような推計は表示側で行い、実測と混ぜない。
    """
    moving, stationary = split_by_speed(vessels)
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")

    return {
        "updated_at": now,
        # 実測か推計かを、source 文字列ではなくこのフィールドで判別する。
        # 表示側は measurement を見る。
        "measurement": "actual",
        "source": "VesselAPI",
        "source_url": "https://vesselapi.com/",
        "fetch_ok": True,
        # 0 隻だったときに「本当に船がいない」と言い切れるか。
        # 対照海域で API の生存を確認できた場合のみ true になる。
        "coverage_verified": coverage["verified"],
        "coverage_check": coverage["detail"],
        "bbox": dict(BBOX),
        "time_window": "api_default",
        "vessels_detected": len(vessels),
        "breakdown": {
            "moving": moving,
            "stationary": stationary,
        },
        "moving_threshold_kt": MOVING_KT,
        "position_reports": reports,
        "pages_fetched": pages,
        "truncated": truncated,
        "note": (
            "VesselAPI の bounding-box エンドポイントから取得した実測値。"
            "位置報告を MMSI で重複排除したユニーク隻数。"
            "対地速力 {}kt 以上を移動中、それ未満を停泊・漂泊として計上。"
            "本エンドポイントは船種を返さないため、タンカー／貨物船の内訳は取得できない。"
            "AIS を停波した船（ダークシッピング）は原理的に含まれない。"
        ).format(MOVING_KT),
    }


def save_snapshot(snapshot, path="data/ais-snapshot.json"):
    """一時ファイルに書いてから置き換える。

    open(path, "w") は開いた時点で原本を空にするため、書き込み中の例外で
    データを失う。GitHub Actions 上でも同じ事故が起こりうる。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    body = json.dumps(snapshot, ensure_ascii=False, indent=2)
    body.encode("utf-8")  # 書き込む前にエンコード可能なことを確かめる

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    print("[AIS] {} を更新しました".format(path))


def main():
    key = os.environ.get("VESSELAPI_API_KEY")
    if not key:
        print("[AIS] ERROR: VESSELAPI_API_KEY が設定されていません。", file=sys.stderr)
        return 2

    try:
        vessels, reports, pages, truncated = collect(key, BBOX, "ホルムズ海峡")
        coverage = verify_coverage(key, len(vessels))
    except FetchError as e:
        print("[AIS] ERROR: {}".format(e), file=sys.stderr)
        print("[AIS] ファイルは更新しません（前回値を残します）。", file=sys.stderr)
        return e.code

    snapshot = build_snapshot(vessels, reports, pages, truncated, coverage)
    save_snapshot(snapshot)

    print("[AIS] 完了: ユニーク {} 隻（移動中 {} / 停泊・漂泊 {}）、"
          "位置報告 {} 件、消費コール {}".format(
              snapshot["vessels_detected"],
              snapshot["breakdown"]["moving"],
              snapshot["breakdown"]["stationary"],
              reports, pages))
    if not coverage["verified"]:
        print("[AIS] 注意: 健全性を確認できませんでした。"
              "表示側は推計へフォールバックします。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
