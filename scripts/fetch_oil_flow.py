"""
fetch_oil_flow.py
財務省貿易統計（e-Stat API）の相手国別 原油輸入数量から
data/oil-flow.json の日本向け調達フローを算出して更新する。

算出方法の詳細は docs/oil-flow-method.md を参照。

必要環境変数:
  ESTAT_APP_ID : e-Stat のアプリケーションID

対象統計表の構造（2026-09-04 に実測）:
  cat01 概況品目(輸入)          : 30301000 = 30301_原油及び粗油
  cat02 概況品目表の数量・金額  : 100=単位 / 110=合計_数量 / 130=1月_数量 …（月ごとに20刻み）
  area  国                      : 50137=サウジ 50147=UAE 50304=米国 50410=ブラジル …
  time  時間軸(年次)            : 2026000000 = 2026年（1年につき1表）

  **月は時間軸ではなく cat02 に入っている。** 時間軸は年しか持たない。

方針:
  - 分類コードは実行時に getMetaInfo で解決する（年ごとの体系変更に耐えるため）
  - 未公表の月を掴まないよう、実際に数値が入っている最新月を選ぶ
  - 年初は当年表にまだデータが無いため、1つ前の年の表へ遡る
  - 既存 JSON の label / color / note / 手動ルート（old, D）は保持する
  - どのルートにも割り当てられない相手国の量は集計に含めない
"""

import os
import re
import sys
import json
import calendar
import unicodedata
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
CRUDE_NAME = "原油及び粗油"      # 概況品目の名称（コードは年により桁数が変わりうる）

BBL_PER_KL = 6.28981            # 1キロリットルあたりのバレル数
VLCC_CAPACITY_BBL = 2000000     # VLCC 1隻あたりの標準積載量（バレル）
EXPECTED_UNIT = "KL"            # 想定する数量の単位。異なれば換算が狂うので中断する
STALE_AFTER_DAYS = 45
MAX_TABLES_TO_TRY = 2           # 当年表にデータが無い場合、前年表まで遡る

JST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "..", "data", "oil-flow.json")
METHOD_URL = "https://github.com/yattanda/hormuz-data-/blob/main/docs/oil-flow-method.md"
ATTRIBUTION = "出典：政府統計の総合窓口(e-Stat)（財務省 普通貿易統計）を加工して作成"

# ルート → 相手国名（e-Stat の国名表記に含まれる文字列で照合する）
ROUTE_MAPPING = {
    "A":    ["アラブ首長国連邦"],
    "B":    ["サウジアラビア"],
    "C_US": ["アメリカ合衆国"],
    "C_GL": ["ブラジル", "ナイジェリア", "アンゴラ"],
}
# 統計から分離できないため手動値のまま据え置くルート
MANUAL_ROUTES = ["old", "D"]

ALL_COUNTRIES = [c for names in ROUTE_MAPPING.values() for c in names]

MONTH_QUANTITY_RE = re.compile(r"^(\d{1,2})月_数量$")
UNIT_NAME = "単位"


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


def find_tables() -> list:
    """「概況品別国別表 輸入」の統計表を新しい順に返す。"""
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
    return tables


def resolve_classes(stats_data_id: str) -> dict:
    """メタ情報から、概況品・国・月・年の分類コードを解決する。"""
    body = api_get("getMetaInfo", {"statsDataId": stats_data_id})
    class_objs = as_list(body["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"])

    resolved = {
        "crude": None,        # (class_id, code)
        "countries": {},      # 国名 -> (class_id, code)
        "months": {},         # 月(int) -> code
        "month_key": None,
        "unit_code": None,
        "year": None,
    }

    for obj in class_objs:
        key = obj["@id"]
        for c in as_list(obj.get("CLASS")):
            code = str(c.get("@code", ""))
            name = str(c.get("@name", ""))

            # 概況品目: 名称で照合する（コードは年により桁数が変わりうる）
            if CRUDE_NAME in name:
                resolved["crude"] = (key, code)

            # 国
            for country in ALL_COUNTRIES:
                if country in name and country not in resolved["countries"]:
                    resolved["countries"][country] = (key, code)

            # 月別の数量（cat02）
            m = MONTH_QUANTITY_RE.match(name)
            if m:
                resolved["months"][int(m.group(1))] = code
                resolved["month_key"] = key
            elif name == UNIT_NAME:
                resolved["unit_code"] = code

            # 年（時間軸）
            if key.startswith("time"):
                digits = "".join(ch for ch in code if ch.isdigit())
                if len(digits) >= 4:
                    resolved["year"] = int(digits[:4])

    missing = []
    if not resolved["crude"]:
        missing.append("概況品目「{}」".format(CRUDE_NAME))
    for country in ALL_COUNTRIES:
        if country not in resolved["countries"]:
            missing.append("国「{}」".format(country))
    if not resolved["months"]:
        missing.append("月別数量（「N月_数量」）")
    if resolved["year"] is None:
        missing.append("時間軸（年）")
    if missing:
        raise RuntimeError(
            "メタ情報から解決できませんでした: " + " / ".join(missing)
            + "  → tools/diag_estat.py meta <id> で実体を確認してください"
        )
    return resolved


def parse_value(raw):
    """統計値を float にする。未公表・秘匿（'-' '***' 等）は None を返す。"""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_monthly(stats_data_id: str, resolved: dict):
    """相手国別・月別の原油輸入数量[kL]を取り、数値が入っている最新月を返す。

    戻り値: (quantities, month)。データが1か月も無ければ (None, None)。
    """
    crude_key, crude_code = resolved["crude"]
    country_key = next(iter(resolved["countries"].values()))[0]
    country_codes = ",".join(code for _, code in resolved["countries"].values())
    month_key = resolved["month_key"]

    month_codes = list(resolved["months"].values())
    if resolved["unit_code"]:
        month_codes = month_codes + [resolved["unit_code"]]

    params = {
        "statsDataId": stats_data_id,
        param_name(crude_key): crude_code,
        param_name(country_key): country_codes,
        param_name(month_key): ",".join(month_codes),
        "metaGetFlg": "N",
        "cntGetFlg": "N",
        "limit": 100000,
    }
    body = api_get("getStatsData", params)
    values = as_list(body["STATISTICAL_DATA"]["DATA_INF"]["VALUE"])
    if not values:
        raise RuntimeError("データが0件です。分類コードの指定を確認してください")

    code_to_country = {code: country for country, (_, code) in resolved["countries"].items()}
    code_to_month = {code: month for month, code in resolved["months"].items()}
    country_attr = "@" + country_key
    month_attr = "@" + month_key

    # 単位の検証。想定と違えば換算係数が狂うので中断する。
    if resolved["unit_code"]:
        units = {str(v.get("$")).strip() for v in values
                 if v.get(month_attr) == resolved["unit_code"]}
        units = {u for u in units if u and u != "None"}
        # 実データの単位は全角の「ＫＬ」。NFKC で半角に正規化してから比較する。
        normalized = {unicodedata.normalize("NFKC", u).upper() for u in units}
        if units and not any(EXPECTED_UNIT in u for u in normalized):
            raise RuntimeError(
                "数量の単位が想定（{}）と異なります: {}".format(EXPECTED_UNIT, sorted(units))
            )

    by_month = {}
    for v in values:
        month = code_to_month.get(v.get(month_attr))
        country = code_to_country.get(v.get(country_attr))
        if month is None or country is None:
            continue
        by_month.setdefault(month, {})[country] = parse_value(v.get("$"))

    # 対象国のいずれかに数値が入っている月だけを「公表済み」とみなす。
    # 6か国すべてが欠測になることは実務上ないため、この判定で未公表月を除ける。
    published = sorted(m for m, d in by_month.items()
                       if any(x is not None for x in d.values()))
    if not published:
        return None, None

    latest = published[-1]
    quantities = {c: (by_month[latest].get(c) or 0.0) for c in ALL_COUNTRIES}
    print("  公表済みの月: {} → 採用 {}月".format(published, latest))
    return quantities, latest


def kl_to_man_bpd(kl: float, year: int, month: int) -> float:
    days = calendar.monthrange(year, month)[1]
    return kl * BBL_PER_KL / days / 10000


def man_bpd_to_tankers_week(man_bpd: float) -> int:
    return round(man_bpd * 10000 * 7 / VLCC_CAPACITY_BBL)


def collect_latest_data():
    """新しい表から順に見て、最初にデータが取れた (統計表ID, 年, 月, 数量) を返す。"""
    tables = find_tables()
    tried = []
    for table in tables[:MAX_TABLES_TO_TRY]:
        table_id = table["@id"]
        print("統計表を確認: {}".format(table_id))
        resolved = resolve_classes(table_id)
        print("  分類キー: 概況品={} / 国={} / 月={} / 年={}".format(
            resolved["crude"][0],
            next(iter(resolved["countries"].values()))[0],
            resolved["month_key"],
            resolved["year"],
        ))
        quantities, month = fetch_monthly(table_id, resolved)
        if quantities:
            return table_id, resolved["year"], month, quantities
        tried.append(table_id)
        print("  公表済みの月が無いため次の表へ")
    raise RuntimeError(
        "データが入った月が見つかりませんでした（確認した表: {}）".format(tried)
    )


def main():
    if not APP_ID:
        print("ESTAT_APP_ID が未設定です", file=sys.stderr)
        sys.exit(1)

    with open(OUT_PATH, encoding="utf-8") as f:
        current = json.load(f)

    table_id, year, month, quantities = collect_latest_data()

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
        "attribution": ATTRIBUTION,
        "provider": "財務省 普通貿易統計「概況品別国別表 輸入」（e-Stat API v3.0）",
        "stats_data_id": table_id,
        "commodity": CRUDE_NAME,
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
