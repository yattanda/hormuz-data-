"""
fetch_eia.py
EIA Open Data API から WTI価格・原油在庫・生産量・中東輸入比率を取得し
data/eia-weekly.json に保存する。

必要環境変数:
  EIA_API_KEY : EIA API キー
"""

import requests
import json
import os
import sys
from datetime import datetime, timezone, timedelta

EIA_BASE = "https://api.eia.gov/v2"

def get_latest_series(api_key: str, series_id: str, frequency: str = "weekly"):
    url = f"{EIA_BASE}/seriesid/{series_id}"
    params = {
        "api_key": api_key,
        "frequency": frequency,
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 2,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("response", {}).get("data", [])
    except Exception as e:
        print(f"[EIA] Error fetching {series_id}: {e}", file=sys.stderr)
        return None

def fetch_all(api_key: str) -> dict:
    results = {}

    # WTI 週次スポット価格
    wti_data = get_latest_series(api_key, "PET.RWTC.W")
    if wti_data and len(wti_data) >= 1:
        results["wti_price_usd"] = float(wti_data[0]["value"])
        results["series_week"] = wti_data[0]["period"]
    else:
        results["wti_price_usd"] = None
        results["series_week"] = None

    # Brent 週次スポット価格
    brent_data = get_latest_series(api_key, "PET.RBRTE.W")
    if brent_data and len(brent_data) >= 1:
        results["brent_price_usd"] = float(brent_data[0]["value"])
    else:
        results["brent_price_usd"] = None

    # 米国原油在庫
    inv_data = get_latest_series(api_key, "PET.WCRSTUS1.W")
    if inv_data and len(inv_data) >= 2:
        inv_current = float(inv_data[0]["value"]) / 1000
        inv_prev    = float(inv_data[1]["value"]) / 1000
        results["us_crude_inventory_mb"]        = round(inv_current, 1)
        results["us_crude_inventory_change_mb"] = round(inv_current - inv_prev, 1)
    else:
        results["us_crude_inventory_mb"]        = None
        results["us_crude_inventory_change_mb"] = None

    # 米国原油生産量
    prod_data = get_latest_series(api_key, "PET.WCRFPUS2.W")
    if prod_data and len(prod_data) >= 1:
        results["us_crude_production_mbpd"] = round(float(prod_data[0]["value"]) / 1000, 1)
    else:
        results["us_crude_production_mbpd"] = None

    # 中東原油輸入比率（月次）
    me_data  = get_latest_series(api_key, "PET.MCRIMXX2.M", frequency="monthly")
    all_data = get_latest_series(api_key, "PET.MCRIMUS2.M", frequency="monthly")
    if me_data and all_data and len(me_data) >= 1 and len(all_data) >= 1:
        me_val  = float(me_data[0]["value"])
        all_val = float(all_data[0]["value"])
        if all_val > 0:
            results["middle_east_import_share_pct"] = round((me_val / all_val) * 100, 1)
        else:
            results["middle_east_import_share_pct"] = None
    else:
        results["middle_east_import_share_pct"] = None

    return results

def build_eia_json(data: dict) -> dict:
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")
    return {
        "updated_at": now,
        "source": "EIA Open Data API v2",
        **data,
    }

def save_eia(eia: dict, path: str = "data/eia-weekly.json"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(eia, f, ensure_ascii=False, indent=2)
    print(f"[EIA] Saved to {path}")

def main():
    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        print("[EIA] ERROR: EIA_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("[EIA] Fetching from EIA Open Data API...")
    data = fetch_all(api_key)
    eia = build_eia_json(data)
    save_eia(eia)

    print(f"[EIA] Done.")
    print(f"  WTI:        ${eia.get('wti_price_usd')} USD/bbl")
    print(f"  Brent:      ${eia.get('brent_price_usd')} USD/bbl")
    print(f"  Inventory:  {eia.get('us_crude_inventory_mb')} MB")
    print(f"  Production: {eia.get('us_crude_production_mbpd')} MBPD")
    print(f"  ME Import%: {eia.get('middle_east_import_share_pct')}%")

if __name__ == "__main__":
    main()
