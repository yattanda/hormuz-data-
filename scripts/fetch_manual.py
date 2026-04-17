"""
fetch_manual.py
RSSフィードから最新ニュースを取得し、
Google Gemini API で分析して
data/manual-update.json を自動更新する。

必要環境変数:
  GEMINI_API_KEY : Google Gemini API キー
"""

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types

# RSS フィードリスト
RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml",
    "https://news.google.com/rss/search?q=Hormuz+strait+blockade&hl=en-US&gl=US&ceid=US:en",
]

KEYWORDS = ["Hormuz", "Iran", "blockade", "oil", "tanker", "ceasefire", "封鎖"]

def fetch_rss_news(max_items=15):
    """RSSフィードからホルムズ関連ニュースを取得"""
    news_items = []
    for url in RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                tree = ET.parse(response)
                root = tree.getroot()
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    desc = item.findtext("description", "")
                    pub_date = item.findtext("pubDate", "")
                    text = f"{title} {desc}"
                    if any(kw.lower() in text.lower() for kw in KEYWORDS):
                        news_items.append({
                            "title": title,
                            "description": desc[:200],
                            "pubDate": pub_date
                        })
            if len(news_items) >= max_items:
                break
        except Exception as e:
            print(f"[RSS] Error fetching {url}: {e}", file=sys.stderr)
    return news_items[:max_items]

def analyze_with_gemini(api_key, news_items):
    """Gemini API でニュースを分析してJSONを生成"""
    client = genai.Client(api_key=api_key)

    news_text = "\n".join([
        f"- {item['title']}: {item['description']}"
        for item in news_items
    ])

    prompt = f"""
あなたはホルムズ海峡・イラン情勢の専門アナリストです。
以下の最新ニュースを分析して、JSON形式で回答してください。
必ずJSON形式のみで返答し、説明文は不要です。

【重要な背景知識】
- 2026年4月13日にCENTCOMがホルムズ海峡封鎖を実施
- 通常時のホルムズ通過量は約21百万バレル/日
- 封鎖前の通過量は約17〜18百万バレル/日
- 現在は大幅に減少していると推定される
- 戦争リスク保険料率は封鎖前の1%から大幅上昇中
- 米イラン間で停戦交渉が進行中（パキスタン仲介）
- 停戦期限は2026年4月22日

【最新ニュース】
{news_text}

【出力形式】
{{
  "scenario": {{
    "A_diplomacy_pct": <外交解決・封鎖解除シナリオの確率 0-100の整数>,
    "B_partial_blockade_pct": <部分封鎖継続シナリオの確率 0-100の整数>,
    "C_full_blockade_pct": <完全封鎖継続シナリオの確率 0-100の整数>,
    "D_escalation_pct": <軍事エスカレーションシナリオの確率 0-100の整数>
  }},
  "war_risk_premium_pct": <戦争リスク保険料率 小数点1桁 現在は1.0〜7.5%の範囲>,
  "war_risk_premium_source": "推定値（Gemini自動分析）",
  "hormuz_daily_flow_mbpd": <ホルムズ通過量 百万バレル/日 封鎖中は2〜10程度>,
  "hormuz_normal_flow_mbpd": 21.0,
  "flow_disruption_pct": <流量disruption率 整数 = round((1 - hormuz_daily_flow_mbpd / 21.0) * 100)>,
  "critical_date": "<次の重要日程 必ずYYYY-MM-DD形式 2026年以降の日付>",
  "critical_note": "<重要日程の説明 日本語30文字以内>",
  "last_manual_note": "<最新状況メモ 日本語100文字以内 ホルムズ封鎖・イラン情勢に関する最新動向>"
}}

【注意事項】
- シナリオ確率の合計は必ず100になること
- critical_dateは必ず2026年以降のYYYY-MM-DD形式
- hormuz_daily_flow_mbpdは封鎖中なので21.0にはならない
- flow_disruption_pctはhormuz_daily_flow_mbpdから計算すること
- last_manual_noteはホルムズ・イラン情勢に関する内容のみ記載
"""


    try:
        response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
        )
        text = response.text.strip()
        # コードブロックを除去
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Gemini] Error: {e}", file=sys.stderr)
        return None

def build_manual_json(data):
    """manual-update.json の形式に変換"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")
    return {
        "updated_at": now,
        "auto_generated": True,
        "source": "Gemini AI自動分析（Reuters/BBC/Al Jazeera RSS）",
        **data
    }

def save_manual(data, path="data/manual-update.json"):
    """JSONファイルに保存"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[Manual] Saved to {path}")

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Manual] ERROR: GEMINI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("[Manual] Fetching RSS news...")
    news_items = fetch_rss_news()
    print(f"[Manual] Found {len(news_items)} relevant articles.")

    if not news_items:
        print("[Manual] No news found. Skipping update.", file=sys.stderr)
        sys.exit(0)

    print("[Manual] Analyzing with Gemini...")
    data = analyze_with_gemini(api_key, news_items)

    if not data:
        print("[Manual] Gemini analysis failed. Skipping update.", file=sys.stderr)
        sys.exit(1)

    manual = build_manual_json(data)
    save_manual(manual)

    print("[Manual] Done.")
    print(f"  シナリオA: {data['scenario']['A_diplomacy_pct']}%")
    print(f"  保険料率: {data['war_risk_premium_pct']}%")
    print(f"  流量: {data['hormuz_daily_flow_mbpd']} MBPD")
    print(f"  重要日程: {data['critical_date']}")

if __name__ == "__main__":
    main()
