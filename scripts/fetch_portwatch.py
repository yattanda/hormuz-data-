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
  3  想定外のレスポンス（項目名・海峡の ID が変わった、行が無い、
     ArcGIS のエラー応答が試し直しても続いた 等）
  4  接続エラー・HTTP エラー

  ※ 0 以外を返した場合はファイルを書かない。前回値がそのまま残り、
     表示側は latest_date から鮮度を判断する。
"""

import json
import os
import re
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
# 日次の行は日付ごとの統計の和で取る（fetch_daily の注記）
DAILY_SUM_FIELDS = ["n_total", "n_tanker", "n_cargo", "capacity_tanker", "capacity"]
STAT_FIELDS = {"total": "n_total", "tanker": "n_tanker", "cargo": "n_cargo",
               "capacity_tanker_t": "capacity_tanker"}

TIMEOUT_SEC = 60
# ArcGIS は同じ問い合わせが通ったり 400 になったりする（2026-11-06 の手元の試しでは
# 最初の1回で通るのは3〜4割）。しかもエラーの応答にも Cache-Control: max-age=30 が付いて
# 配信の途中でキャッシュされることがあり、30秒以内に取り直すと同じエラーが返る
# （X-Cache: TCP_HIT を確認）。間をキャッシュより長くあける。
# 外れは時間でかたまって続く（11/6 の手元の実行で9回続けて外れ10回目で通った）ので、
# 後の回ほど間を広げる
RETRY_WAIT_SEC = 35        # 400 などの2〜4回目の前（キャッシュの30秒より長く）
RETRY_WAIT_LONG_SEC = 60   # 400 などの5回目以降の前
# 429 は IMF の ArcGIS の組織全体の上限（1分あたりの request units）を超えたとき。
# ほかの利用者と分け合う上限で、こちらの問い合わせ（1回3単位ほど）のせいではない。
# 11/6・11/7 の定時は、最初の問い合わせから約10分ずっと 429 だった（応答は「Retry after 60 sec」）。
# 指示より長めに待ち、試し直しは回数でなく実行全体の持ち時間で区切る
THROTTLE_WAIT_SEC = 90
# 実行全体（問い合わせ4つ）で試し直しに使ってよい時間。ワークフローの timeout-minutes（50）より短く
RETRY_BUDGET_SEC = 40 * 60
# 応答ヘッダー x-esri-org-request-units-per-min の usage/max がこれ以上なら、
# 次の問い合わせの前に1分待つ（上限の手前で 429 をもらいに行かない）
ORG_USAGE_HIGH = 0.9
ORG_USAGE_WAIT_SEC = 60
JST = timezone(timedelta(hours=9))
OUT_PATH = "data/portwatch-hormuz.json"


class FetchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def warn(message):
    """GitHub Actions の注記として出す（手元ではただの1行）。"""
    print("::warning::[PortWatch] " + message)


_deadline = None    # 最初の問い合わせのときに決める（RETRY_BUDGET_SEC）
_org_usage = None   # 直前の応答の (usage, max)


def org_usage(headers):
    """x-esri-org-request-units-per-min: usage=5993;max=10000 → (5993, 10000)。無ければ None。"""
    raw = headers.get("x-esri-org-request-units-per-min") if headers else None
    if not raw:
        return None
    try:
        parts = dict(kv.split("=", 1) for kv in raw.replace(" ", "").split(";"))
        return int(parts["usage"]), int(parts["max"])
    except (ValueError, KeyError):
        return None


def usage_text():
    return "（組織の使用量 {}/{}）".format(*_org_usage) if _org_usage else ""


def retry_after(error):
    """429 の details の「Retry after N sec」から N を読む。読めなければ None。"""
    m = re.search(r"Retry after (\d+)", " ".join(str(d) for d in error.get("details") or []))
    return int(m.group(1)) if m else None


def query(params, label):
    global _deadline, _org_usage
    if _deadline is None:
        _deadline = time.monotonic() + RETRY_BUDGET_SEC
    params = dict(params, f="json")
    url = BASE + "?" + urllib.parse.urlencode(params)

    # 直前の応答で組織の上限が近ければ、少し待ってから問い合わせる
    if _org_usage and _org_usage[0] >= _org_usage[1] * ORG_USAGE_HIGH:
        print("[PortWatch] 組織の上限が近いので {} 秒待ちます{}".format(ORG_USAGE_WAIT_SEC, usage_text()))
        time.sleep(ORG_USAGE_WAIT_SEC)

    last = None
    arcgis_error = None
    attempt = 0
    wait = 0
    while True:
        if attempt > 0:
            if time.monotonic() + wait > _deadline:
                break
            time.sleep(wait)
        attempt += 1
        headers = None
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as res:
                headers = res.headers
                body = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _org_usage = org_usage(e.headers) or _org_usage
            last = e
            if e.code == 429:
                arcgis_error = {"code": 429, "message": str(e)}
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = max(THROTTLE_WAIT_SEC, int(ra) + 30 if ra and ra.isdigit() else 0)
            else:
                wait = RETRY_WAIT_SEC if attempt <= 3 else RETRY_WAIT_LONG_SEC
            print("[PortWatch] {} の取得に失敗（{}回目）: {}{}".format(label, attempt, e, usage_text()))
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            wait = RETRY_WAIT_SEC if attempt <= 3 else RETRY_WAIT_LONG_SEC
            print("[PortWatch] {} の取得に失敗（{}回目）: {}".format(label, attempt, e))
            continue
        _org_usage = org_usage(headers) or _org_usage
        # ArcGIS はエラーでも HTTP 200 で {"error": {...}} を返す。
        # 同じ問い合わせが通ったり 400 になったりすることがあるので、試し直す
        # （2026-10-29：定時と手動の実行がともに 400 で失敗。10/31〜11/5 は定時7回のうち4回、
        # 5秒〜20秒あけた5回がすべて外れた。間がキャッシュの30秒より短かったため。上の RETRY_WAIT_SEC）
        # URL に毎回違う値を足してキャッシュを避けると、かえって通りにくい（11/6 の試し：
        # 足さない 7/10・8/10、足す 1/10・4/10。通った応答のキャッシュも使えなくなるため）
        if "error" in body:
            arcgis_error = body["error"]
            if arcgis_error.get("code") == 429:
                ra = retry_after(arcgis_error)
                wait = max(THROTTLE_WAIT_SEC, ra + 30 if ra else 0)
            else:
                wait = RETRY_WAIT_SEC if attempt <= 3 else RETRY_WAIT_LONG_SEC
            print("[PortWatch] {} で ArcGIS のエラー応答（{}回目）: {}{}".format(label, attempt, arcgis_error, usage_text()))
            continue
        print("[PortWatch] {} を取得（{}回目）{}".format(label, attempt, usage_text()))
        return body
    print("[PortWatch] 試し直しの持ち時間（{}分）を使い切りました".format(RETRY_BUDGET_SEC // 60))
    if arcgis_error is not None:
        raise FetchError(3, "{} で ArcGIS のエラー応答: {}".format(label, arcgis_error))
    raise FetchError(4, "{} に接続できませんでした: {}".format(label, last))


def where(start, end=None):
    clause = "portid = '{}' AND date >= DATE '{}'".format(PORT_ID, start)
    if end:
        clause += " AND date <= DATE '{}'".format(end)
    return clause


def fetch_latest_date():
    # orderByFields と resultRecordCount を一緒に付けると 400 になりやすかった（2026-11-06 の
    # 1秒おき12回の試し：並べて1件取る形は 0/12、統計の max は 12/12）。ただし統計の形でも
    # 外れが続く時間はあるので、決め手は上の試し直し。最新日は統計で取る
    body = query({
        "where": "portid = '{}'".format(PORT_ID),
        "outStatistics": json.dumps([{"statisticType": "max", "onStatisticField": "date",
                                      "outStatisticFieldName": "latest"}]),
    }, "最新日")
    features = body.get("features") or []
    latest = features[0]["attributes"].get("latest") if features else None
    if not latest:
        raise FetchError(3, "{} の行がありません（海峡の ID が変わった可能性）".format(PORT_ID))
    return date.fromisoformat(latest)


def fetch_daily():
    # 範囲は今日からではなく最新の行から数える。今日から数えると、PortWatch の
    # 更新が止まったあいだも先頭の行が1つずつ落ち、中身の無いコミットが毎日できる
    latest = fetch_latest_date()
    first = latest - timedelta(days=DAILY_DAYS - 1)
    # 行そのもの（outFields）では取らず、日付ごとの統計（groupByFieldsForStatistics）で取る。
    # 行を返す問い合わせには、IMF の組織全体で 1分 6000 request units の別の上限があり、
    # 2026-11-06〜11-07 はそこが埋まって 429 が何時間も続いた（統計の問い合わせは
    # 上限 10000 の側で通った。11/7 の手元の試し）。海峡と日付で1行なので、和＝その日の値。
    # 11/7 に、この形で取った60日分がコミット済みの daily と同じであることを確かめた。
    # 並べ替えと件数の指定は付けない（上の fetch_latest_date の注記）。並べるのはこちらで行う
    stats = [{"statisticType": "sum", "onStatisticField": f, "outStatisticFieldName": f}
             for f in DAILY_SUM_FIELDS]
    stats.append({"statisticType": "count", "onStatisticField": "n_total",
                  "outStatisticFieldName": "rows"})
    body = query({
        "where": where(first.isoformat(), latest.isoformat()),
        "outStatistics": json.dumps(stats),
        "groupByFieldsForStatistics": "date,portid,portname",
    }, "日次 {}〜{}".format(first, latest))
    if body.get("exceededTransferLimit"):
        raise FetchError(3, "{}〜{} の行がサービスの上限を超えました（一部しか返っていない）".format(first, latest))
    rows = sorted((f["attributes"] for f in body.get("features", [])), key=lambda r: r["date"])
    if not rows:
        raise FetchError(3, "{}〜{} の行がありません".format(first, latest))
    missing = [k for k in DAILY_FIELDS if k not in rows[0]]
    if missing:
        raise FetchError(3, "項目がありません: {}".format(missing))
    doubled = [r["date"] for r in rows if r.get("rows") != 1]
    if doubled:
        raise FetchError(3, "1日に1行ではない日があります（和が日の値になりません）: {}".format(doubled[:5]))
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
    body = query({"where": where(start, end), "outStatistics": json.dumps(stats)},
                 "基準 {}〜{}".format(start, end))
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
