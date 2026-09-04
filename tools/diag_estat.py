"""
diag_estat.py — e-Stat API の分類コードを実測して確定するための診断ツール

fetch_oil_flow.py が使う以下を、実際のAPIレスポンスから特定する。
  - 統計表ID（statsDataId）: 「概況品別国別表 輸入」の最新年版
  - 概況品コード: 30301（原油及び粗油）が入っている分類キー（cat01 等）
  - 国コード: UAE / サウジアラビア / 米国 / ブラジル / ナイジェリア / アンゴラ
  - 時間軸コード: 最新の月次データ

必要環境変数:
  ESTAT_APP_ID : e-Stat のアプリケーションID（https://www.e-stat.go.jp/api/ で登録）

使い方:
  python tools/diag_estat.py list      # 統計表を検索して statsDataId 候補を出す
  python tools/diag_estat.py meta <statsDataId>   # その表のメタ情報（分類コード）を出す
"""

import os
import sys
import json
import requests

BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"
APP_ID = os.getenv("ESTAT_APP_ID")

# 財務省貿易統計の政府統計コード
STATS_CODE_TRADE = "00350300"

TARGET_COUNTRIES = [
    "アラブ首長国連邦",
    "サウジアラビア",
    "アメリカ合衆国",
    "ブラジル",
    "ナイジェリア",
    "アンゴラ",
]


def require_app_id():
    if not APP_ID:
        print("ESTAT_APP_ID が未設定です。https://www.e-stat.go.jp/api/ で登録してください。",
              file=sys.stderr)
        sys.exit(1)


def cmd_list():
    """概況品別国別表 輸入 の統計表を検索する。"""
    params = {
        "appId": APP_ID,
        "statsCode": STATS_CODE_TRADE,
        "searchWord": "概況品別国別表 輸入",
        "limit": 50,
    }
    r = requests.get(f"{BASE}/getStatsList", params=params, timeout=60)
    r.raise_for_status()
    body = r.json()["GET_STATS_LIST"]

    status = body["RESULT"]["STATUS"]
    if status != 0:
        print(f"APIエラー STATUS={status}: {body['RESULT'].get('ERROR_MSG')}", file=sys.stderr)
        sys.exit(1)

    tables = body["DATALIST_INF"]["TABLE_INF"]
    if isinstance(tables, dict):
        tables = [tables]

    print(f"{len(tables)} 件")
    for t in tables:
        title = t.get("TITLE")
        title = title.get("$") if isinstance(title, dict) else title
        print(f"  id={t['@id']}  周期={t.get('SURVEY_DATE')}  {title}")


def cmd_meta(stats_data_id: str):
    """統計表のメタ情報から、必要な分類コードを抜き出す。"""
    params = {"appId": APP_ID, "statsDataId": stats_data_id}
    r = requests.get(f"{BASE}/getMetaInfo", params=params, timeout=60)
    r.raise_for_status()
    body = r.json()["GET_META_INFO"]

    status = body["RESULT"]["STATUS"]
    if status != 0:
        print(f"APIエラー STATUS={status}: {body['RESULT'].get('ERROR_MSG')}", file=sys.stderr)
        sys.exit(1)

    class_objs = body["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
    if isinstance(class_objs, dict):
        class_objs = [class_objs]

    for obj in class_objs:
        key, name = obj["@id"], obj["@name"]
        classes = obj.get("CLASS", [])
        if isinstance(classes, dict):
            classes = [classes]
        print(f"\n=== {key} : {name}  ({len(classes)} 項目) ===")

        # 原油（30301）を探す
        hits = [c for c in classes if "30301" in str(c.get("@code", ""))
                or "原油" in str(c.get("@name", ""))]
        # 対象国を探す
        hits += [c for c in classes
                 if any(country in str(c.get("@name", "")) for country in TARGET_COUNTRIES)]
        # 時間軸は末尾を出す
        if not hits and key.startswith("time"):
            hits = classes[:5]

        for c in hits[:20]:
            print(f"    code={c['@code']}  name={c['@name']}")
        if not hits:
            print(f"    （該当なし。先頭3件: "
                  f"{[ (c['@code'], c['@name']) for c in classes[:3] ]}）")


if __name__ == "__main__":
    require_app_id()
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "list":
        cmd_list()
    elif sys.argv[1] == "meta" and len(sys.argv) >= 3:
        cmd_meta(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
