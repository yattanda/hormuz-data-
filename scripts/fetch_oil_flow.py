"""
fetch_oil_flow.py
財務省貿易統計（e-Stat API）の相手国別 原油輸入数量から
data/oil-flow.json の日本向け調達フローを算出して更新する。

算出方法の詳細は docs/oil-flow-method.md を参照。

必要環境変数:
  ESTAT_APP_ID : e-Stat のアプリケーションID

方針:
  - 分類コードは実行時に getMetaInfo で解決する（年ごとの体系変更に耐えるため）
  - 既存 JSON の label / color / note / 手動ルート（old, D）は保持する
  - どのルートにも割り当てられない相手国の量は集計に含めない
"""

import os
import sys
import json
import calendar
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"


def load_env_file():
    """リポジトリ直下の .env から環境変数を読む（ローカル検証用）。

    GitHub Actions では secrets が環境変数として渡るため、その場合は何もしない。
    python-dotenv に依存しないよう最小限の実装にしている。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
APP_ID = os.getenv("ESTAT_APP_ID")

STATS_CODE_TRADE = "00350300"   # 財務省貿易統計
CRUDE_CODE = "30301"            # 概況品: 原油及び粗油

BBL_PER_KL = 6.28981            # 1キロリットルあたりのバレル数
VLCC_CAPACITY_BBL = 2000000     # VLCC 1隻あたりの標準積載量（バレル）
STALE_AFTER_DAYS = 45

JST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "..", "data", "oil-flow.json")
METHOD_URL = "https://github.com/yattanda/hormuz-data-/blob/main/docs/oil-flow-method.md"

# ルート → 相手国名（e-Stat の国名表記に含まれる文字列で照合する）
ROUTE_MAPPING = {
    "A":    ["アラブ首長国連邦"],
    "B":    ["サウジアラビア"],
    "C_US": ["アメリカ合衆国"],
    "C_GL": ["ブラジル", "ナイジェリア", "アンゴラ"],
}
# 統計から分離できないため手動値のまま据え置くルート
MANUAL_ROUTES = ["old", "D"]


def api_get(endpoint: str, params: dict) -> dict:
    """e-Stat API を叩き、ルート直下のオブジェクトを返す。"""
    params = dict(params)
    params["appId"] = APP_ID
    r = requests.get(BASE + "/" + endpoint, params=params, timeout=60)
    r.raise_for_status()
    payload = r.json()
    body = payload[next(iter(payload))]
    result = body.get("RESULT", {})
    if result.get("STATUS") != 0:
        raise RuntimeError(
            "e-Stat {} STATUS={}: {}".format(
                endpoint, result.get("STATUS"), result.get("ERROR_MSG")
            )
        )
    return body


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def param_name(class_id: str) -> str:
    """分類ID（cat01 等）を getStatsData の絞り込みパラメータ名に変換する。"""
    return "cd" + class_id[0].upper() + class_id[1:]


def find_latest_table() -> str:
    """「概況品別国別表 輸入」の最新の統計表IDを返す。"""
    body = api_get("getStatsList", {
        "statsCode": STATS_CODE_TRADE,
        "searchWord": "概況品別国別表 輸入",
        "limit": 100,
    })
    tables = as_list(body["DATALIST_INF"]["TABLE_INF"])
    if not tables:
        raise RuntimeError("概況品別国別表 輸入 が見つかりません")

    def sort_key(t):
        return str(t.get("SURVEY_DATE") or t.get("UPDATED_DATE") or "")

    tables.sort(key=sort_key, reverse=True)
    return tables[0]["@id"]


def resolve_classes(stats_data_id: str) -> dict:
    """メタ情報から、概況品・国・時間軸の分類キーとコードを解決する。"""
    body = api_get("getMetaInfo", {"statsDataId": stats_data_id})
    class_objs = as_list(body["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"])

    resolved = {"crude": None, "countries": {}, "time_key": None}

    for obj in class_objs:
        key = obj["@id"]
        classes = as_list(obj.get("CLASS"))

        for c in classes:
            code = str(c.get("@code", ""))
            name = str(c.get("@name", ""))
            if code == CRUDE_CODE or (CRUDE_CODE in code and "原油" in name):
                resolved["crude"] = (key, code)
            for countries in ROUTE_MAPPING.values():
                for country in countries:
                    if country in name and country not in resolved["countries"]:
                        resolved["countries"][country] = (key, code)

        if key.startswith("time"):
            resolved["time_key"] = key

    missing = []
    if not resolved["crude"]:
        missing.append("概況品コード " + CRUDE_CODE)
    for countries in ROUTE_MAPPING.values():
        for country in countries:
            if country not in resolved["countries"]:
                missing.append("国「{}」".format(country))
    if missing:
        raise RuntimeError(
            "メタ情報から解決できませんでした: " + " / ".join(missing)
            + "  → tools/diag_estat.py meta <id> で実体を確認してください"
        )
    return resolved


def fetch_quantities(stats_data_id: str, resolved: dict):
    """相手国別の原油輸入数量[kL]と、対象期間コードを返す。"""
    crude_key, crude_code = resolved["crude"]
    country_key = next(iter(resolved["countries"].values()))[0]
    country_codes = ",".join(code for _, code in resolved["countries"].values())

    params = {
        "statsDataId": stats_data_id,
        param_name(crude_key): crude_code,
        param_name(country_key): country_codes,
        "metaGetFlg": "N",
        "cntGetFlg": "N",
        "limit": 10000,
    }
    body = api_get("getStatsData", params)

    values = as_list(body["STATISTICAL_DATA"]["DATA_INF"]["VALUE"])
    if not values:
        raise RuntimeError("データが0件です。分類コードの指定を確認してください")

    code_to_country = {code: country for country, (_, code) in resolved["countries"].items()}
    country_attr = "@" + country_key
    time_attr = "@" + (resolved["time_key"] or "time")

    all_periods = sorted({v[time_attr] for v in values if time_attr in v})
    if not all_periods:
        raise RuntimeError("時間軸が取得できませんでした")

    periods = [p for p in all_periods if is_monthly_period(p)]
    if not periods:
        raise RuntimeError(
            "月次の時間軸コードが見つかりません。取得できたコード: "
            + ", ".join(all_periods[-10:])
        )
    latest = periods[-1]
    print("時間軸: 月次 {} 件 / 全 {} 件 → 採用 {}".format(
        len(periods), len(all_periods), latest))

    quantities = {}
    for v in values:
        if v.get(time_attr) != latest:
            continue
        country = code_to_country.get(v.get(country_attr))
        if not country:
            continue
        try:
            quantities[country] = float(v.get("$"))
        except (TypeError, ValueError):
            quantities[country] = 0.0

    return quantities, latest


def is_monthly_period(period_code: str) -> bool:
    """月次の時間軸コードか判定する。

    同じ表に年計（末尾が 00 の「2026000000」など）が混ざっており、
    単純に最大値を取ると年計を掴んでしまうため必要。
    """
    digits = "".join(ch for ch in str(period_code) if ch.isdigit())
    if len(digits) < 6:
        return False
    return 1 <= int(digits[-2:]) <= 12


def period_to_year_month(period_code: str):
    """e-Stat の時間軸コードから年・月を取り出す。

    月次の時間軸コードは「2026000707」のように
    年4桁 + 期間種別 + 月2桁（末尾）で構成される。
    """
    digits = "".join(ch for ch in str(period_code) if ch.isdigit())
    if len(digits) < 6:
        raise RuntimeError("時間軸コードを解釈できません: " + str(period_code))
    year = int(digits[:4])
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise RuntimeError(
            "月次データではない時間軸コードです（月={}）: {}".format(month, period_code)
        )
    return year, month


def kl_to_man_bpd(kl: float, year: int, month: int) -> float:
    days = calendar.monthrange(year, month)[1]
    return kl * BBL_PER_KL / days / 10000


def man_bpd_to_tankers_week(man_bpd: float) -> int:
    return round(man_bpd * 10000 * 7 / VLCC_CAPACITY_BBL)


def main():
    if not APP_ID:
        print("ESTAT_APP_ID が未設定です", file=sys.stderr)
        sys.exit(1)

    with open(OUT_PATH, encoding="utf-8") as f:
        current = json.load(f)

    table_id = find_latest_table()
    print("統計表ID:", table_id)
    resolved = resolve_classes(table_id)
    print("分類キー: 概況品={} / 国={}".format(
        resolved["crude"][0], next(iter(resolved["countries"].values()))[0]))
    quantities, period = fetch_quantities(table_id, resolved)
    year, month = period_to_year_month(period)

    routes = current["routes"]
    audit_quantities = {}
    total_mapped = 0.0

    for route, countries in ROUTE_MAPPING.items():
        kl = sum(quantities.get(c, 0.0) for c in countries)
        man_bpd = kl_to_man_bpd(kl, year, month)
        total_mapped += man_bpd

        entry = dict(routes.get(route, {}))
        entry["prev_bpd"] = entry.get("bpd", 0)
        entry["bpd"] = round(man_bpd)
        entry["tankers_week"] = man_bpd_to_tankers_week(man_bpd)
        entry["derivation"] = "estimated_from_trade_statistics"
        routes[route] = entry

        for c in countries:
            audit_quantities[c] = round(quantities.get(c, 0.0))

    for route in MANUAL_ROUTES:
        if route in routes:
            routes[route]["derivation"] = "manual"

    now = datetime.now(JST)
    current["_comment"] = (
        "月次自動更新（財務省貿易統計・e-Stat API）。BPDは万バレル/日単位。"
        "tankers_week は VLCC 換算の必要隻数であり実寄港隻数ではない。"
        "prev_bpd は前回更新時の値。算出方法は docs/oil-flow-method.md を参照。"
        "note / status は現在どの表示側でも未使用。"
    )
    current["updated"] = "{}年{}月分（財務省貿易統計）".format(year, month)
    current["updated_iso"] = now.isoformat(timespec="seconds")
    current["stale_after_days"] = STALE_AFTER_DAYS
    current["method"] = {
        "version": "2.0",
        "url": METHOD_URL,
        "bbl_per_kl": BBL_PER_KL,
        "vlcc_capacity_bbl": VLCC_CAPACITY_BBL,
        "formula_bpd": "bpd[万BPD] = 数量[kL] × 6.28981 ÷ 当月日数 ÷ 10000",
        "formula_tankers": "tankers_week = bpd × 10000 × 7 ÷ 2000000",
    }
    current["route_mapping"] = ROUTE_MAPPING
    current["source"] = {
        # 政府標準利用規約（第2.0版）に基づく出典表示。
        # 数量を換算・集計しているため「加工して作成」に当たる。
        "attribution": "出典：政府統計の総合窓口(e-Stat)（財務省 普通貿易統計）を加工して作成",
        "provider": "財務省 普通貿易統計「概況品別国別表 輸入」（e-Stat API v3.0）",
        "stats_data_id": table_id,
        "commodity_code": CRUDE_CODE,
        "period_code": period,
        "period": "{}-{:02d}".format(year, month),
        "fetched_at": now.isoformat(timespec="seconds"),
    }
    current["_audit"] = {
        "quantities_kl": audit_quantities,
        "mapped_total_bpd": round(total_mapped, 1),
        "note": "quantities_kl は各相手国の原油輸入数量[kL]。ルート未対応国は集計に含めていない。",
    }

    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, OUT_PATH)

    print("更新完了: {}年{}月分 / 統計表={}".format(year, month, table_id))
    for route in ROUTE_MAPPING:
        r = routes[route]
        print("  {}: {}万BPD  {}隻/週".format(route, r["bpd"], r["tankers_week"]))


if __name__ == "__main__":
    main()
