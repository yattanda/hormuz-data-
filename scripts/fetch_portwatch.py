"""
fetch_portwatch.py
IMF PortWatch の Daily Chokepoint Transit Calls から、ホルムズ海峡の
日次通航数と積載量（推定）を取得し、data/portwatch-hormuz.json に保存する。

このファイルは data/portwatch-hormuz.json の **唯一の書き手** である。
AI による推計（data/ais-estimate.json・scripts/fetch_manual.py）とは混ぜない。
設計は docs/data-sources-redesign.md を参照。

PortWatch について（2026-10-25 に確認）:
  - 衛星を含む AIS の信号から、海峡の境界を越えた船を「通航」として数える。
    数日にまたがる通航は1回とし、同じ船は 48 時間あけてから再び数える
  - 対象はコンテナ・ばら積み・一般貨物・RoRo・タンカーの5種類だけ。
    AIS を切った船（ダーク船）は数に入らないので、値は「AIS で捕捉した分」＝下限
  - capacity_* は船の寸法・喫水などから推定した積載量（トン）
  - 約1週間遅れで、週1回まとめて入るとみられる（10/25 の最新は 10/18）
  - 利用条件は IMF の Copyright and Terms（データ項目の licenseInfo）。
    出典の表示が必須。商用での再利用は IMF の許可が要る

必要環境変数: なし（公開の ArcGIS Feature Service。キー不要）

終了コード:
  0  取得成功（中身が前回と同じならファイルを書かない）
  3  想定外のレスポンス（項目名・海峡の ID が変わった、行が無い 等）
  4  接続エラー・HTTP エラー

  ※ 0 以外を返した場合はファイルを書かない。前回値がそのまま残り、
     表示側は latest_date から鮮度を判断する。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
        "Daily_Chokepoints_Data/FeatureServer/0/query")
DATASET_URL = "https://portwatch.imf.org/datasets/3da2b9ca97684916b75c4013f95d18ab_0/about"
TERMS_URL = "https://www.imf.org/external/terms.htm"

PORT_ID = "chokepoint6"
PORT_NAME = "Strait of Hormuz"

# IMF の条件どおりの出典と、サイトに出す日本語の出典。
# 7日平均・平常比を出すので「加工」に当たる。
ATTRIBUTION = "Source: International Monetary Fund, PortWatch, " + DATASET_URL
ATTRIBUTION_JA = "出典：IMF PortWatch（International Monetary Fund）を加工して作成"

# 日次の行を何日ぶん持つか（カードの小さな折れ線用）
DAILY_DAYS = 60
# 主な値は直近 WINDOW_DAYS 日の平均。日ごとの揺れ（0〜7隻）が大きいため
WINDOW_DAYS = 7

# 平常時の基準。2026年1〜2月は開戦前でも 2025年より2割低く、緊張の高まりが
# 入っているとみて使わない。上半期は EIA の平常時流量（20.9・2025年上半期）と
# 同じ期で、積載量から流量への換算に使う（docs/data-sources-redesign.md §5-2）
BASELINES = {
    "2025": ("2025-01-01", "2025-12-31"),
    "2025H1": ("2025-01-01", "2025-06-30"),
}
# 基準が前回からこれ以上動いたら警告する（PortWatch が過去の値を直した可能性）
BASELINE_DRIFT_WARN = 0.05

# latest_date がこれより古ければ警告する（週1回の更新を2回落とした目安）
STALE_AFTER_DAYS = 14
# 通航が0隻の日がこれだけ続いたら警告する（1日の0は実際にある）
ZERO_RUN_WARN_DAYS = 7

DAILY_FIELDS = ["date", "portid", "portname", "n_total", "n_tanker", "n_cargo",
                "capacity_tanker", "capacity"]
STAT_FIELDS = {"total": "n_total", "tanker": "n_tanker", "cargo": "n_cargo",
               "capacity_tanker_t": "capacity_tanker"}

TIMEOUT_SEC = 60
RETRIES = 3
JST = timezone(timedelta(hours=9))
OUT_PATH = "data/portwatch-hormuz.json"


class FetchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def warn(message):
    """GitHub Actions の注記として出す（手元ではただの1行）。"""
    print("::warning::[PortWatch] " + message)


def query(params):
    params = dict(params, f="json")
    url = BASE + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as res:
                body = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            print("[PortWatch] 取得に失敗（{}/{}）: {}".format(attempt, RETRIES, e))
            time.sleep(5 * attempt)
            continue
        # ArcGIS はエラーでも HTTP 200 で {"error": {...}} を返す
        if "error" in body:
            raise FetchError(3, "ArcGIS のエラー応答: {}".format(body["error"]))
        return body
    raise FetchError(4, "接続できませんでした: {}".format(last))


def where(start, end=None):
    clause = "portid = '{}' AND date >= DATE '{}'".format(PORT_ID, start)
    if end:
        clause += " AND date <= DATE '{}'".format(end)
    return clause


def fetch_latest_date():
    body = query({
        "where": "portid = '{}'".format(PORT_ID),
        "outFields": "date",
        "orderByFields": "date DESC",
        "resultRecordCount": 1,
    })
    features = body.get("features") or []
    if not features:
        raise FetchError(3, "{} の行がありません（海峡の ID が変わった可能性）".format(PORT_ID))
    return date.fromisoformat(features[0]["attributes"]["date"])


def fetch_daily():
    # 範囲は今日からではなく最新の行から数える。今日から数えると、PortWatch の
    # 更新が止まったあいだも先頭の行が1つずつ落ち、中身の無いコミットが毎日できる
    latest = fetch_latest_date()
    first = latest - timedelta(days=DAILY_DAYS - 1)
    body = query({
        "where": where(first.isoformat(), latest.isoformat()),
        "outFields": ",".join(DAILY_FIELDS),
        "orderByFields": "date ASC",
        "resultRecordCount": 1000,
    })
    rows = [f["attributes"] for f in body.get("features", [])]
    if not rows:
        raise FetchError(3, "{}〜{} の行がありません".format(first, latest))
    missing = [k for k in DAILY_FIELDS if k not in rows[0]]
    if missing:
        raise FetchError(3, "項目がありません: {}".format(missing))
    if rows[0]["portname"] != PORT_NAME:
        raise FetchError(3, "{} の名前が {} ではありません: {}".format(
            PORT_ID, PORT_NAME, rows[0]["portname"]))
    return [{
        "date": r["date"],
        "total": r["n_total"],
        "tanker": r["n_tanker"],
        "cargo": r["n_cargo"],
        "capacity_tanker_t": r["capacity_tanker"],
        "capacity_t": r["capacity"],
    } for r in rows]


def fetch_baseline(start, end):
    stats = [{"statisticType": "avg", "onStatisticField": field, "outStatisticFieldName": key}
             for key, field in STAT_FIELDS.items()]
    stats.append({"statisticType": "count", "onStatisticField": "n_total",
                  "outStatisticFieldName": "days"})
    body = query({"where": where(start, end), "outStatistics": json.dumps(stats)})
    features = body.get("features") or []
    if not features:
        raise FetchError(3, "基準 {}〜{} の集計が返りませんでした".format(start, end))
    a = features[0]["attributes"]
    expected = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if not a.get("days") or a["days"] < expected * 0.95:
        raise FetchError(3, "基準 {}〜{} の日数が足りません: {}/{}".format(
            start, end, a.get("days"), expected))
    return {
        "period": "{}/{}".format(start, end),
        "days": a["days"],
        "total": round(a["total"], 1),
        "tanker": round(a["tanker"], 1),
        "cargo": round(a["cargo"], 1),
        "capacity_tanker_t": round(a["capacity_tanker_t"]),
    }


def window_average(daily):
    """latest_date で終わる直近 WINDOW_DAYS 日の平均。"""
    latest = date.fromisoformat(daily[-1]["date"])
    first = (latest - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = [r for r in daily if r["date"] >= first]
    if len(rows) < WINDOW_DAYS:
        warn("直近 {} 日のうち {} 日ぶんしか行がありません".format(WINDOW_DAYS, len(rows)))

    def avg(key, ndigits=None):
        return round(sum(r[key] for r in rows) / len(rows), ndigits)

    return {
        "period": "{}/{}".format(first, daily[-1]["date"]),
        "days": len(rows),
        "total": avg("total", 1),
        "tanker": avg("tanker", 1),
        "cargo": avg("cargo", 1),
        "capacity_tanker_t": avg("capacity_tanker_t"),
        # 船はいるのに積載量が 0 の日（推定が付かなかった日）。多い週は積載量を割り引いて読む
        "tanker_days_without_capacity": sum(
            1 for r in rows if r["tanker"] > 0 and not r["capacity_tanker_t"]),
    }


def check(snapshot, previous, today):
    latest = date.fromisoformat(snapshot["latest_date"])
    age = (today - latest).days
    if age > STALE_AFTER_DAYS:
        warn("最新の行が {}（{} 日前）です。PortWatch の更新が止まっていないか確かめてください".format(
            latest, age))

    run = 0
    for r in reversed(snapshot["daily"]):
        if r["total"]:
            break
        run += 1
    if run >= ZERO_RUN_WARN_DAYS:
        warn("通航0隻が {} 日続いています。PortWatch の画面で確かめてください".format(run))

    for key, base in snapshot["baselines"].items():
        old = ((previous or {}).get("baselines") or {}).get(key)
        if not old:
            continue
        for field in ("total", "capacity_tanker_t"):
            if old[field] and abs(base[field] - old[field]) / old[field] > BASELINE_DRIFT_WARN:
                warn("基準 {} の {} が {} → {} に動きました（PortWatch が過去の値を直した可能性）".format(
                    key, field, old[field], base[field]))


def build_snapshot(daily, baselines):
    return {
        "fetched_at": None,  # 書き込むときに入れる
        "measurement": "actual",
        "source": "IMF PortWatch（Daily Chokepoint Transit Calls）",
        "source_url": DATASET_URL,
        "terms_url": TERMS_URL,
        "attribution": ATTRIBUTION,
        "attribution_ja": ATTRIBUTION_JA,
        "chokepoint": {"portid": PORT_ID, "portname": PORT_NAME},
        "latest_date": daily[-1]["date"],
        "window": window_average(daily),
        "baselines": baselines,
        "daily": daily,
        "note": ("AIS で捕捉した通航の集計（IMF PortWatch）。AIS を切った船は含まない下限。"
                 "対象はコンテナ・ばら積み・一般貨物・RoRo・タンカーの5種類。"
                 "capacity_* は PortWatch が喫水などから推定した積載量（トン）。"
                 "日付は UTC。fetched_at は中身が最後に変わった日時。"),
    }


def load_previous(path=OUT_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def same_content(a, b):
    if not a or not b:
        return False
    strip = lambda d: {k: v for k, v in d.items() if k != "fetched_at"}
    return strip(a) == strip(b)


def save(snapshot, path=OUT_PATH):
    """一時ファイルに書いてから置き換える（書き込み中の例外で原本を失わないため）。"""
    body = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, path)


def main():
    today = datetime.now(timezone.utc).date()
    try:
        daily = fetch_daily()
        baselines = {key: fetch_baseline(*span) for key, span in BASELINES.items()}
    except FetchError as e:
        print("[PortWatch] ERROR: {}".format(e), file=sys.stderr)
        print("[PortWatch] ファイルは更新しません（前回値を残します）。", file=sys.stderr)
        return e.code

    previous = load_previous()
    snapshot = build_snapshot(daily, baselines)
    check(snapshot, previous, today)

    w = snapshot["window"]
    base = baselines["2025"]
    print("[PortWatch] 最新 {}・直近{}日平均 {} 隻/日（タンカー {}）・2025年平均 {} 隻/日の {:.1%}".format(
        snapshot["latest_date"], w["days"], w["total"], w["tanker"], base["total"],
        w["total"] / base["total"]))

    if same_content(snapshot, previous):
        print("[PortWatch] 変更なし。{} は書き換えません".format(OUT_PATH))
        return 0

    snapshot["fetched_at"] = datetime.now(JST).isoformat(timespec="seconds")
    save(snapshot)
    print("[PortWatch] {} を更新しました".format(OUT_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
