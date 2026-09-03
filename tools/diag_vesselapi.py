"""
diag_vesselapi.py - VesselAPI (vesselapi.com) 実現可能性の検証ツール
（読み取り専用・ファイル書き込みなし・標準ライブラリのみ）

目的:
    AISstream.io にペルシャ湾のカバレッジが無いと確定したため（2026-09-01 実測）、
    代替候補である VesselAPI の無料枠（150コール/月）が、ホルムズ海峡の通航船を
    実際に返すのかを、契約・実装の前に実測で確かめる。

    「カバレッジを検証せずに採用した」ことが 4.5 か月ぶんの 0 隻を招いた。
    同じ失敗を繰り返さないための事前検証にあたる。

前提:
    https://vesselapi.com/ でサインアップ（カード不要）し、ダッシュボードで API キーを発行する。
    キーの有効期限は最長 90 日。運用に乗せる場合は必ず 90 日を選ぶこと。

使い方（PowerShell）:
    $env:VESSELAPI_API_KEY = "xxxxx"
    python diag_vesselapi.py

    python diag_vesselapi.py --key xxxxx        環境変数の代わりに直接指定
    python diag_vesselapi.py --hours 4          time.from/to を明示指定（最大4時間）
    python diag_vesselapi.py --skip-controls    対照海域の実験を省略（コール節約）
    python diag_vesselapi.py --verbose          レスポンス本文を長めに表示

消費コール数:
    既定で最大 5 コール（ホルムズ1・対照3・認証テスト1）。無料枠 150 に対して十分小さい。
    --skip-controls なら 2 コール。

終了コード:
    0  ホルムズ海峡で船舶を検出できた   -> VesselAPI は代替として成立する
    10 ホルムズ0隻 / 対照海域は受信あり -> 当該海域のカバレッジが無い（AISstream と同じ）
    20 対照海域も0隻                    -> API 側またはアカウント側の問題
    30 接続・認証エラー
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.vesselapi.com/v1/location/vessels/bounding-box"

# API 側の制約: bbox の総スパン |dLat| + |dLon| は 4 度以内
# （2026-09-03 に実測で判明。公開ドキュメントには記載が無い）
SPAN_LIMIT = 4.0

# ホルムズ海峡の本体と主要通航路。スパン和 1.2 + 1.4 = 2.6 度で制約内に収まる。
# 本番 scripts/fetch_ais.py の BBOX（lat 22-27 / lon 55.5-60.5）はスパン和 10 度で
# この API では通らない。オマーン湾・UAE沿岸まで含めるにはタイル分割が要る（README 参照）
HORMUZ = {"latBottom": 25.8, "latTop": 27.0, "lonLeft": 55.6, "lonRight": 57.0}

# 対照海域。上2つは世界有数の輻輳海域で、AIS が機能していれば必ず船が返る
CONTROLS = [
    ("シンガポール海峡", {"latBottom": 1.0, "latTop": 1.4, "lonLeft": 103.4, "lonRight": 104.0}),
    ("ロッテルダム港沖", {"latBottom": 51.8, "latTop": 52.2, "lonLeft": 3.8, "lonRight": 4.5}),
    # 船がまず居ない海域。「取得成功かつ0隻」の応答形を確認するための対照
    ("南太平洋（空白対照）", {"latBottom": -39.5, "latTop": -39.0, "lonLeft": -139.5, "lonRight": -139.0}),
]

TIMEOUT = 30


def span_of(box):
    """bbox の総スパン |dLat| + |dLon| を返す。"""
    return abs(box["latTop"] - box["latBottom"]) + abs(box["lonRight"] - box["lonLeft"])


def check_spans():
    """API を叩く前に、全 bbox がスパン制約を満たすか自己検証する。"""
    ng = []
    for label, box in [("ホルムズ海峡", HORMUZ)] + CONTROLS:
        s = span_of(box)
        if s > SPAN_LIMIT:
            ng.append("{}: スパン和 {:.1f} 度（上限 {}）".format(label, s, SPAN_LIMIT))
    return ng


def call(key, box, hours=None, limit=50, verbose=False):
    """bounding-box を1回叩く。(status, headers, body_dict_or_text) を返す。"""
    params = {
        "filter.lonLeft": box["lonLeft"],
        "filter.lonRight": box["lonRight"],
        "filter.latBottom": box["latBottom"],
        "filter.latTop": box["latTop"],
        "pagination.limit": limit,
    }
    if hours:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        params["time.from"] = (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        params["time.to"] = now.isoformat().replace("+00:00", "Z")

    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            raw = res.read().decode("utf-8", "replace")
            status, headers = res.status, dict(res.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status, headers = e.code, dict(e.headers)
    except Exception as e:
        return None, {}, "接続エラー: {}: {}".format(type(e).__name__, e)

    if verbose:
        print("      URL: " + url)
        print("      RAW: " + raw[:800])

    try:
        return status, headers, json.loads(raw)
    except json.JSONDecodeError:
        return status, headers, raw


def quota_of(headers):
    """X-RateLimit-Remaining を取り出す。大文字小文字を問わない。"""
    for k, v in headers.items():
        if k.lower() == "x-ratelimit-remaining":
            return v
    return None


def source_of(headers):
    """X-Data-Source を取り出す。terrestrial か satellite かの判別に使う。"""
    for k, v in headers.items():
        if k.lower() == "x-data-source":
            return v
    return None


def probe(label, key, box, hours, verbose):
    """1海域を叩いて結果を要約表示し、(隻数, ヘッダ, body) を返す。隻数 None は失敗。"""
    print("")
    print("[{}]".format(label))
    status, headers, body = call(key, box, hours=hours, verbose=verbose)

    if status is None:
        print("  -> 失敗: {}".format(body))
        return None, headers, body

    src = source_of(headers)
    rem = quota_of(headers)
    print("  HTTP {}  X-Data-Source={}  X-RateLimit-Remaining={}".format(
        status, src if src else "(なし)", rem if rem else "(なし)"))

    if status != 200:
        if isinstance(body, dict):
            err = body.get("error") or {}
            print("  -> エラー: code={} message={}".format(err.get("code", ""), err.get("message", "")))
        else:
            print("  -> エラー本文: {}".format(str(body)[:300]))
        return None, headers, body

    if not isinstance(body, dict):
        print("  -> 想定外のレスポンス形式: {}".format(str(body)[:300]))
        return None, headers, body

    vessels = body.get("vessels") or []
    n = len(vessels)
    more = bool(body.get("nextToken"))
    print("  -> 取得成功。{} 隻（nextToken: {}）".format(n, "あり = ページング必要" if more else "なし"))

    if n:
        keys = sorted(vessels[0].keys())
        print("     フィールド: {}".format(", ".join(keys)))
        shiptype = [k for k in keys if "type" in k.lower()]
        print("     船種フィールド: {}".format(", ".join(shiptype) if shiptype else "なし（breakdown を作れない）"))
        v = vessels[0]
        print("     例: {} (MMSI {}) lat={} lon={} sog={} ts={}".format(
            v.get("vessel_name"), v.get("mmsi"), v.get("latitude"),
            v.get("longitude"), v.get("sog"), v.get("timestamp")))

    return n, headers, body


def main():
    ap = argparse.ArgumentParser(description="VesselAPI のホルムズ海峡カバレッジを実測する")
    ap.add_argument("--key", default=os.environ.get("VESSELAPI_API_KEY"))
    ap.add_argument("--hours", type=int, default=None,
                    help="time.from/to を明示指定する（最大4。既定は指定なし）")
    ap.add_argument("--skip-controls", action="store_true", help="対照海域の実験を省略する")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.key:
        print("APIキーがありません。$env:VESSELAPI_API_KEY を設定するか --key で渡してください。")
        return 30
    if args.hours is not None and not (1 <= args.hours <= 4):
        print("--hours は 1〜4 の範囲で指定してください（API 側の上限が4時間）。")
        return 30

    ng = check_spans()
    if ng:
        print("bbox がスパン制約（|dLat|+|dLon| <= {} 度）に違反しています。".format(SPAN_LIMIT))
        for line in ng:
            print("  - " + line)
        return 30

    print("=" * 72)
    print("VesselAPI 実現可能性検証  {}".format(
        datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M JST")))
    print("bbox(ホルムズ): lat {} - {} / lon {} - {}（スパン和 {:.1f} 度）".format(
        HORMUZ["latBottom"], HORMUZ["latTop"], HORMUZ["lonLeft"], HORMUZ["lonRight"],
        span_of(HORMUZ)))
    print("time 窓: {}".format("直近{}時間".format(args.hours) if args.hours else "指定なし（API既定）"))
    print("=" * 72)

    hormuz_n, headers, body = probe("本命: ホルムズ海峡", args.key, HORMUZ, args.hours, args.verbose)
    quota_before = quota_of(headers)

    if hormuz_n is None:
        # 認証・権限エラーはここで打ち切る。対照を叩いてもクォータの無駄
        if isinstance(body, dict):
            code = (body.get("error") or {}).get("code", "")
            if code in ("invalid_api_key", "feature_not_available"):
                print("")
                print("判定: 認証・権限のエラーです。キーの有効期限（最長90日）と")
                print("      プランで当該エンドポイントが使えるかを確認してください。")
                return 30
        print("")
        print("判定: リクエストが失敗しました。上記のエラー内容を確認してください。")
        return 30

    control_total = 0
    quota_after = quota_before
    if not args.skip_controls:
        for label, box in CONTROLS:
            n, h, _ = probe("対照: " + label, args.key, box, args.hours, args.verbose)
            q = quota_of(h)
            if q is not None:
                quota_after = q
            if n and "空白対照" not in label:
                control_total += n

    # 認証の失敗形を確認する。「0隻」と「失敗」が区別できるかの実証
    print("")
    print("[認証テスト: 意図的に不正なキーで1回叩く]")
    st, h, b = call("invalid-key-for-diagnosis", HORMUZ, hours=args.hours)
    if st is None:
        print("  -> 接続エラー: {}".format(b))
    else:
        code = (b.get("error") or {}).get("code", "") if isinstance(b, dict) else ""
        print("  HTTP {}  code={}".format(st, code))
        if st in (401, 403):
            print("  -> 取得失敗は HTTP {} で明確に判別できる。".format(st))
            print("     「取得成功かつ0隻（HTTP 200 / vessels: []）」と構造的に区別可能。")
        else:
            print("  -> 想定外。失敗と0隻の区別可否を手動で確認すること。")

    print("")
    print("=" * 72)
    print("結果まとめ")
    print("  ホルムズ海峡: {} 隻".format(hormuz_n))
    if not args.skip_controls:
        print("  対照海域合計: {} 隻".format(control_total))
    if quota_before is not None and quota_after is not None:
        print("  クォータ残: {} -> {}".format(quota_before, quota_after))
    print("=" * 72)

    if hormuz_n > 0:
        print("")
        print("判定: ホルムズ海峡で実データを取得できました。")
        print("      VesselAPI は代替ソースとして成立します。")
        print("      次は (1) 公開サイトでの表示が規約上許されるかを提供元に確認、")
        print("           (2) 船種フィールドの有無を踏まえた breakdown 表示の設計、")
        print("           (3) ページングを含めた月間コール数の試算（無料枠150）を行ってください。")
        return 0
    if args.skip_controls:
        print("")
        print("判定: ホルムズ 0 隻。対照を省略したため原因を切り分けられていません。")
        print("      --skip-controls を外して再実行してください。")
        return 10
    if control_total > 0:
        print("")
        print("判定: ホルムズ 0 隻 / 対照海域では受信あり。")
        print("      当該海域のカバレッジが存在しない可能性が高い（AISstream.io と同じ結末）。")
        print("      無料枠は地上AISのみで、衛星応答には別枠の衛星クレジットが要る旨が")
        print("      ドキュメントに記載されている。有料の衛星クレジットで解決するかは")
        print("      提供元に確認しない限り不明。")
        return 10
    print("")
    print("判定: 対照海域でも 0 隻。API 側またはアカウント側の問題が疑われます。")
    print("      キーの有効期限・プラン・クォータ残を確認してください。")
    return 20


if __name__ == "__main__":
    sys.exit(main())
