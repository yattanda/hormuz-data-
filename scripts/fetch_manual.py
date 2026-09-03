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
    # Google News 検索（ホルムズ・封鎖関連）
    "https://news.google.com/rss/search?q=Hormuz+strait+Iran+blockade+2026&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Hormuz+oil+flow+tanker+2026&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+ceasefire+talks+2026&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+war+oil+price+2026&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=war+risk+insurance+tanker+Hormuz&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=MarineTraffic+Hormuz+ships+2026&hl=en-US&gl=US&ceid=US:en",
    # 一般ニュースRSS
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml",
]


KEYWORDS = ["Hormuz", "Iran", "blockade", "oil", "tanker", "ceasefire", "strait", "封鎖", "ホルムズ", "イラン"]

def fetch_rss_news(max_items=40):
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
    import time
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
- 米イラン間で停戦交渉が進行中（パキスタン仲介）
- 停戦期限は2026年4月22日

【最新ニュース】（{len(news_items)}件のニュース記事を分析）
{news_text}

【出力形式】
{{
  "scenario": {{
    "A_diplomacy_pct": <外交解決・封鎖解除シナリオの確率 0-100の整数>,
    "B_partial_blockade_pct": <部分封鎖継続シナリオの確率 0-100の整数>,
    "C_full_blockade_pct": <完全封鎖継続シナリオの確率 0-100の整数>,
    "D_escalation_pct": <軍事エスカレーションシナリオの確率 0-100の整数>
  }},
  "hormuz_daily_flow_mbpd": <ホルムズ通過量 百万バレル/日 封鎖中は2〜10程度>,
  "hormuz_normal_flow_mbpd": 21.0,
  "flow_disruption_pct": <流量disruption率 整数 = round((1 - hormuz_daily_flow_mbpd / 21.0) * 100)>,
  "critical_date": "<次の重要日程 必ずYYYY-MM-DD形式 2026年以降の日付>",
  "critical_note": "<重要日程の説明 日本語30文字以内>",
  "last_manual_note": "<最新状況メモ 日本語100文字以内 ホルムズ封鎖・イラン情勢に関する最新動向>",
  "ais_estimated_vessels": <整数。対象は緯度25.8〜27.0°N・経度55.6〜57.0°Eのバウンディングボックス内を【通過中】の船舶のみ。待機中・引き返し中・停泊中は除外。通常時は約80隻/日、封鎖中は通常比10〜30%（8〜24隻）が目安>,
  "ais_estimated_tankers": <整数。上記通過中船舶のうちタンカーのみ。通常時比率約60%、封鎖中は大幅減>,
  "ais_estimated_cargo": <整数。上記通過中船舶のうち貨物船のみ>,
  "ais_confidence": "<high / medium / low のいずれか1語。推計の信頼度>",
  "ais_estimation_note": <推計根拠を30文字以内で記載。信頼度の語は含めず根拠のみ。例：「DoD発表・Kpler推計より」>
}}

【注意事項】
- シナリオ確率の合計は必ず100になること
- critical_dateは必ず2026年以降のYYYY-MM-DD形式
- hormuz_daily_flow_mbpdは封鎖中なので21.0にはならない
- flow_disruption_pctはhormuz_daily_flow_mbpdから計算すること
- last_manual_noteはホルムズ・イラン情勢に関する内容のみ記載
- ais_estimated_vesselsは封鎖中のニュース・公開データから推計すること
- ais_estimated_tankers + ais_estimated_cargo <= ais_estimated_vessels
- ais_estimated_vesselsは「通過中」のみカウント。周辺待機・引き返し船は含めない
- 封鎖中のタンカー比率は通常60%だが封鎖中は大幅に下がる可能性がある
- 根拠となるニュースが見つからない場合はais_estimated_vesselsを5〜15の範囲で保守的に推計
- ais_confidenceは必ず high / medium / low のいずれか1語のみ。文章にしない
- 直接の根拠となる報道がなく背景知識からの外挿にとどまる場合は low とすること
"""

    for attempt in range(3):  # 最大3回リトライ
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            text = response.text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            return json.loads(text)
        except Exception as e:
            print(f"[Gemini] Attempt {attempt+1} failed: {e}", file=sys.stderr)
            if attempt < 2:
                wait = (attempt + 1) * 30  # 30秒→60秒待機
                print(f"[Gemini] Waiting {wait}s before retry...", file=sys.stderr)
                time.sleep(wait)

    print("[Gemini] All retries failed.", file=sys.stderr)
    return None

def build_manual_json(data):
    """manual-update.json の形式に変換"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")
    return {
        "updated_at": now,
        "auto_generated": True,
        "source": "Gemini AI自動分析（Google ニュース検索・BBC・Al Jazeera・NYT の公開RSS）",
        **data
    }

def write_json(data, path):
    """一時ファイルに書いてから置き換える。

    open(path, "w") は開いた時点で原本を空にするため、書き込み中に例外が出ると
    原本を失う。エンコードの検証も書き込み前に済ませる。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    body = json.dumps(data, ensure_ascii=False, indent=2)
    body.encode("utf-8")  # 書き込む前にエンコード可能なことを確かめる

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)


def save_manual(data, path="data/manual-update.json"):
    """JSONファイルに保存"""
    write_json(data, path)
    print(f"[Manual] Saved to {path}")


def parse_confidence(data):
    """信頼度を high / medium / low に正規化する。

    ais_confidence を優先し、無ければ ais_estimation_note 末尾の "(low)" 形式を拾う。
    プロンプト変更前の出力形式が返ってきても壊れないようにするための二段構え。
    判定できない場合は最も保守的な low を採る。
    """
    raw = str(data.get("ais_confidence") or "").strip().lower()
    if raw in ("high", "medium", "low"):
        return raw

    note = str(data.get("ais_estimation_note") or "").lower()
    for level in ("high", "medium", "low"):
        if f"({level})" in note:
            return level
    return "low"


def strip_confidence(note):
    """推計根拠の文末に付いた "(low)" 等を取り除く。信頼度は別フィールドで持つ。"""
    text = str(note or "").strip()
    for level in ("high", "medium", "low", "High", "Medium", "Low"):
        for token in (f"({level})", f"（{level}）"):
            text = text.replace(token, "")
    return text.strip() or "Gemini AI推計"


def save_ais_estimate(data, path="data/ais-estimate.json"):
    """Gemini 推計を data/ais-estimate.json に保存する。

    **実測ファイル data/ais-snapshot.json には決して書かない。**
    かつて両者が同じファイルを交互に上書きし、実測と推計が区別できない状態が
    2026-04〜09 にわたって続いた。生成元とファイルを1対1に保つことが本関数の要件。

    ダーク船補正（×1.35）はここでは掛けない。補正は推計であり、
    実測・推計の双方に同じ係数を適用する必要があるため、表示側で一元的に扱う。
    """
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")

    vessels = data.get("ais_estimated_vessels", 0)
    tankers = data.get("ais_estimated_tankers", 0)
    cargo = data.get("ais_estimated_cargo", 0)
    confidence = parse_confidence(data)
    reason = strip_confidence(data.get("ais_estimation_note"))

    estimate = {
        "updated_at": now,
        # 実測か推計かを、source 文字列ではなくこのフィールドで判別する
        "measurement": "estimate",
        "source": "Gemini AI推計（ニュース・公開データより）",
        "model": "gemini-2.5-flash",
        "method": "公開RSS報道を Gemini 2.5 Flash が分析した推計",
        "estimated_vessels": vessels,
        "breakdown": {
            "tanker": tankers,
            "cargo": cargo,
            "other": max(0, vessels - tankers - cargo),
        },
        "confidence": confidence,
        "estimation_note": reason,
        "bbox": {
            "lat_bottom": 25.8,
            "lat_top": 27.0,
            "lon_left": 55.6,
            "lon_right": 57.0,
        },
        "note": (
            "AIS 受信による実測値ではない。報道・公開データから生成AIが推計した値であり、"
            "実測が取得できない場合の代替として表示される。"
        ),
    }

    write_json(estimate, path)
    print(f"[AIS] Saved estimate to {path}")
    print(f"  推定船舶数: {vessels} 隻（信頼度: {confidence}）")

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Manual] ERROR: GEMINI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("[Manual] Fetching RSS news...")
    news_items = fetch_rss_news(max_items=40)
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

    # AIS推計は実測とは別ファイル（data/ais-estimate.json）に保存する
    save_ais_estimate(data)

    print("[Manual] Done.")
    print(f"  シナリオA: {data['scenario']['A_diplomacy_pct']}%")
    print(f"  流量: {data['hormuz_daily_flow_mbpd']} MBPD")
    print(f"  重要日程: {data['critical_date']}")

if __name__ == "__main__":
    main()
