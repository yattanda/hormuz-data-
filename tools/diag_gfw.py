"""
diag_gfw.py - Global Fishing Watch API 実現可能性の検証ツール
（読み取り専用・ファイル書き込みなし・標準ライブラリのみ）

目的: AISstream にペルシャ湾のカバレッジが無いと確定したため、
      代替候補である GFW の AIS Vessel Presence データセットが
      ホルムズ海峡の通航船を実際に返すのかを実測で確かめる。

前提: https://globalfishingwatch.org/our-apis/tokens で取得した
      API トークンが必要（アカウント登録は利用者本人が行うこと）。

使い方（PowerShell）:
    $env:GFW_API_TOKEN = "eyJ..."
    python diag_gfw.py

    python diag_gfw.py --days 7        集計する日数（既定7日）
    python diag_gfw.py --lag 5         何日前を終端にするか（既定5日。公称遅延96時間のため）
    python diag_gfw.py --verbose       レスポンス本文を長めに表示

終了コード:
    0  ホルムズ海峡で船舶を検出できた -> GFW は代替として成立する
    10 API は応答するが該当海域が0件 -> 要精査
    20 認証エラー                     -> トークンを確認
    30 エンドポイント／パラメータが不正 -> 出力されたレスポンスを共有してください
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GATEWAY = "https://gateway.api.globalfishingwatch.org"

# 本番 fetch_ais.py と同一の bbox
BBOX = {"min_lat": 22.0, "max_lat": 27.0, "min_lon": 55.5, "max_lon": 60.5}

HORMUZ_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[
        [BBOX["min_lon"], BBOX["min_lat"]],
        [BBOX["max_lon"], BBOX["min_lat"]],
        [BBOX["max_lon"], BBOX["max_lat"]],
        [BBOX["min_lon"], BBOX["max_lat"]],
        [BBOX["min_lon"], BBOX["min_lat"]],
    ]]
}

MAX_BODY_CHARS = 1500
VERBOSE_BODY_CHARS = 6000


def show(title):
    print()
    print("-" * 68)
    print(title)
    print("-" * 68)


def call(method, url, token, body=None, timeout=60):
    """HTTPリクエストを送り、(status, body_text, error) を返す。例外は握りつぶさない。"""
    data = None
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "hormuz-map-diagnostic/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as res:
            return res.status, res.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), None
    except Exception as e:
        return None, "", "{}: {}".format(type(e).__name__, e)


def preview(text, verbose):
    limit = VERBOSE_BODY_CHARS if verbose else MAX_BODY_CHARS
    try:
        parsed = json.loads(text)
        pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pretty = text
    if len(pretty) > limit:
        return pretty[:limit] + "\n...(truncated)"
    return pretty


def probe_auth(token, verbose):
    """トークンが有効かを軽いエンドポイントで確認する。"""
    show("STEP 1: トークンの有効性確認")
    candidates = [
        ("v3", GATEWAY + "/v3/vessels/search?query=hormuz&datasets[0]=public-global-vessel-identity:latest&limit=1"),
        ("v2", GATEWAY + "/v2/vessels/search?query=hormuz&datasets=public-global-vessel-identity:latest&limit=1"),
    ]
    ok_version = None
    for ver, url in candidates:
        status, body, err = call("GET", url, token)
        if err:
            print("  [{}] 通信エラー: {}".format(ver, err))
            continue
        print("  [{}] HTTP {}  {}".format(ver, status, url.split("?")[0]))
        if status == 401 or status == 403:
            print("     -> 認証拒否。レスポンス:")
            print(preview(body, verbose))
            return None, "auth"
        if status == 200:
            ok_version = ver
            print("     -> 認証OK")
            break
        if verbose:
            print(preview(body, verbose))
    return ok_version, None


def probe_report(token, ver, start, end, verbose):
    """4Wings report で AIS Vessel Presence を bbox 集計する。"""
    show("STEP 2: 4Wings report（AIS Vessel Presence）で ホルムズ bbox を集計")
    print("  対象期間: {} 〜 {}".format(start, end))
    print("  bbox: 緯度 {}-{}N / 経度 {}-{}E".format(
        BBOX["min_lat"], BBOX["max_lat"], BBOX["min_lon"], BBOX["max_lon"]))

    date_range = "{},{}".format(start, end)
    versions = [ver] if ver else ["v3", "v2"]

    # パラメータ名は版によって差があるため、複数の組み合わせを順に試す
    param_sets = [
        {
            "datasets[0]": "public-global-presence:latest",
            "date-range": date_range,
            "spatial-resolution": "LOW",
            "temporal-resolution": "ENTIRE",
            "group-by": "VESSEL_ID",
            "format": "JSON",
        },
        {
            "datasets[0]": "public-global-presence:latest",
            "date-range": date_range,
            "spatial-resolution": "LOW",
            "temporal-resolution": "ENTIRE",
            "group-by": "FLAG",
            "format": "JSON",
        },
        {
            "datasets[0]": "public-global-presence:latest",
            "date-range": date_range,
            "spatial-resolution": "LOW",
            "temporal-resolution": "DAILY",
            "format": "JSON",
        },
    ]

    for v in versions:
        for i, params in enumerate(param_sets, 1):
            url = "{}/{}/4wings/report?{}".format(
                GATEWAY, v, urllib.parse.urlencode(params))
            status, body, err = call("POST", url, token, body={"geojson": HORMUZ_GEOJSON})
            label = "[{} / パターン{}]".format(v, i)
            if err:
                print("  {} 通信エラー: {}".format(label, err))
                continue
            print("  {} HTTP {}  group-by={} temporal={}".format(
                label, status, params.get("group-by", "-"), params["temporal-resolution"]))
            if status == 200:
                print("     -> 成功。レスポンス:")
                print(preview(body, verbose))
                return body
            if status in (401, 403):
                print("     -> 認証拒否")
                print(preview(body, verbose))
                return None
            # 400系はパラメータ不一致。中身を見せて次を試す
            print(preview(body, True if verbose else False))
    return None


def summarize(body):
    """レスポンスから船舶数らしき値を抽出して要約する。構造が読めなければそのまま返す。"""
    show("STEP 3: 判定")
    try:
        data = json.loads(body)
    except Exception:
        print("  JSONとして解釈できませんでした。上の生レスポンスを確認してください。")
        return 30

    entries = data.get("entries") if isinstance(data, dict) else None
    if not entries:
        print("  entries が空です。該当期間・該当海域のデータが0件の可能性があります。")
        print("  取得した構造のキー: {}".format(
            list(data.keys()) if isinstance(data, dict) else type(data).__name__))
        return 10

    total = 0
    for entry in entries:
        if isinstance(entry, dict):
            for _, rows in entry.items():
                if isinstance(rows, list):
                    total += len(rows)
    print("  entries 数: {} / 内包レコード数: {}".format(len(entries), total))
    if total > 0:
        print("  -> ホルムズ海峡bboxでデータを取得できました。GFW は代替として成立します。")
        return 0
    print("  -> レコードが0件でした。")
    return 10


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GFW_API_TOKEN"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--lag", type=int, default=5,
                    help="公称96時間の遅延があるため、終端を何日前にするか")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.token:
        print("GFW_API_TOKEN が未設定です。")
        print("https://globalfishingwatch.org/our-apis/tokens で取得し、環境変数か --token で指定してください。")
        return 20

    end = date.today() - timedelta(days=args.lag)
    start = end - timedelta(days=args.days)

    print("GFW API 検証開始  gateway={}  python={}".format(GATEWAY, sys.version.split()[0]))
    print("注意: このAPIは CC BY-NC 4.0（非商用限定）です。収益化する場合は使用できません。")

    ver, authfail = probe_auth(args.token, args.verbose)
    if authfail == "auth":
        print("\n-> トークンが無効か期限切れです。")
        return 20

    body = probe_report(args.token, ver, start.isoformat(), end.isoformat(), args.verbose)
    if body is None:
        show("判定")
        print("  4Wings report を成功させられませんでした。")
        print("  上に出ているレスポンス本文をそのまま共有してください。パラメータを調整します。")
        return 30

    return summarize(body)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断しました。")
        sys.exit(130)
