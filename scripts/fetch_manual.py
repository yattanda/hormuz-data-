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


# 出典表示に使う文字列。footer はこれを並べて「データソース」欄を組み立てるため、
# 1件だけ読んでも供給元・モデル・入力が分かる形にする。表示側に文字列を置かない。
GEMINI_SOURCE = (
    "Gemini 2.5 Flash による推計"
    "（入力: Google ニュース検索・BBC・Al Jazeera・New York Times の公開RSS）"
)

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

def load_context(path="data/context.json"):
    """プロンプトに注入する前提を data/context.json から読み込む。

    前提をコードに直書きすると、状況が変わっても誰かがコードを編集するまで
    出力が追随しない。実際 2026-04-17 から 2026-09-04 まで 141 日間、
    失効した停戦期限がプロンプトに残り続けた。前提はデータとして外に置く。
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 手動でしか確認できない項目。Gemini には生成させない。
# build_manual_json は毎回ファイルを作り直すため、明示的に引き継がないと
# 手動で入れた値が翌日の自動更新で消える。実際 2026-05-15 に保険料率を
# 「手動確認のみ」へ設計変更した後、3フィールドともファイルから失われ、
# 表示は「手動確認時のみ更新 ／ （AIの実行時刻）」のまま空欄になっていた。
MANUAL_ONLY_DEFAULTS = {
    "war_risk_premium_manual": None,
    "war_risk_premium_verified": False,
    "war_risk_premium_source": None,
}


def load_existing_manual(path="data/manual-update.json"):
    """既存の manual-update.json を読む。無い・壊れている場合は空 dict を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[Manual] 既存ファイルを読めませんでした（手動項目は既定値になります）: {e}",
              file=sys.stderr)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def context_age_days(context):
    """前提の基準日からの経過日数。基準日が読めない場合は None を返す。"""
    raw = str(context.get("context_updated") or "")
    try:
        basis = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    jst = timezone(timedelta(hours=9))
    return (datetime.now(jst).date() - basis).days


def build_timeline_text(context):
    """timeline を「- 日付: 事実（出典）」の行に整形する。"""
    lines = []
    for item in context.get("timeline", []):
        date = item.get("date", "")
        fact = item.get("fact", "")
        source = item.get("source", "")
        suffix = f"（出典: {source}）" if source else ""
        lines.append(f"- {date}: {fact}{suffix}")
    return "\n".join(lines) if lines else "- （前提となる事実が登録されていません）"


def build_scenario_text(context):
    """シナリオ定義を「キー: 説明」の行に整形する。"""
    defs = context.get("scenario_definitions", {})
    lines = []
    for key, desc in defs.items():
        if key.startswith("_"):
            continue
        lines.append(f"- {key}: {desc}")
    return "\n".join(lines)


def analyze_with_gemini(api_key, news_items, context):
    """Gemini API でニュースを分析してJSONを生成"""
    import time
    client = genai.Client(api_key=api_key)

    normal_flow = context["normal_flow_mbpd"]["value"]
    context_updated = context.get("context_updated", "不明")
    timeline_text = build_timeline_text(context)
    scenario_text = build_scenario_text(context)

    news_text = "\n".join([
        f"- {item['title']}: {item['description']}"
        for item in news_items
    ])

    prompt = f"""
あなたはホルムズ海峡・イラン情勢の専門アナリストです。
以下の最新ニュースを分析して、JSON形式で回答してください。
必ずJSON形式のみで返答し、説明文は不要です。

【確定した経緯】（基準日 {context_updated}・data/context.json より）
{timeline_text}

【流量に関する前提】
- 通常時のホルムズ通過量は約{normal_flow}百万バレル/日
- 封鎖前の通過量は約17〜18百万バレル/日
- 現在は大幅に減少していると推定される

【最新ニュース】（{len(news_items)}件のニュース記事を分析）
{news_text}

【出力形式】
{{
  "scenario": {{
    "A_diplomacy_pct": <0-100の整数>,
    "B_partial_blockade_pct": <0-100の整数>,
    "C_full_blockade_pct": <0-100の整数>,
    "D_escalation_pct": <0-100の整数>
  }},
  "hormuz_daily_flow_mbpd": <ホルムズ通過量 百万バレル/日 封鎖中は2〜10程度>,
  "hormuz_normal_flow_mbpd": {normal_flow},
  "flow_disruption_pct": <流量disruption率 整数 = round((1 - hormuz_daily_flow_mbpd / {normal_flow}) * 100)>,
  "last_manual_note": "<最新状況メモ 日本語100文字以内 ホルムズ封鎖・イラン情勢に関する最新動向>",
  "ais_estimated_vessels": <整数。対象は緯度25.8〜27.0°N・経度55.6〜57.0°Eのバウンディングボックス内を【通過中】の船舶のみ。待機中・引き返し中・停泊中は除外。通常時は約80隻/日、封鎖中は通常比10〜30%（8〜24隻）が目安>,
  "ais_estimated_tankers": <整数。上記通過中船舶のうちタンカーのみ。通常時比率約60%、封鎖中は大幅減>,
  "ais_estimated_cargo": <整数。上記通過中船舶のうち貨物船のみ>,
  "ais_confidence": "<high / medium / low のいずれか1語。推計の信頼度>",
  "ais_estimation_note": <推計根拠を30文字以内で記載。信頼度の語は含めず根拠のみ。例：「DoD発表・Kpler推計より」>
}}

【シナリオの定義】（表示側のカード見出しと一致させてある。この定義に沿って確率を割り当てること）
{scenario_text}

【注意事項】
- シナリオ確率の合計は必ず100になること
- hormuz_daily_flow_mbpdは封鎖中なので{normal_flow}にはならない
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

def build_manual_json(data, context, previous=None):
    """manual-update.json の形式に変換。

    前提の基準日と経過日数を出力に含める。表示側はこれを見て
    「前提が古い」ことを読者に伝えられる。含めなければ、前提の陳腐化は
    誰にも気付かれないまま出力に効き続ける。

    previous には既存ファイルの内容を渡す。手動確認でしか埋まらない項目
    （MANUAL_ONLY_DEFAULTS）はここから引き継ぐ。
    """
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).isoformat(timespec="seconds")
    age = context_age_days(context)
    stale_after = context.get("stale_after_days")
    prev = previous or {}
    carried = {k: prev.get(k, v) for k, v in MANUAL_ONLY_DEFAULTS.items()}
    return {
        "updated_at": now,
        "auto_generated": True,
        "source": GEMINI_SOURCE,
        "context": {
            "updated": context.get("context_updated"),
            "age_days": age,
            "stale_after_days": stale_after,
            "stale": (age is not None and stale_after is not None and age > stale_after),
            "normal_flow_verified": context["normal_flow_mbpd"].get("verified", False),
            "note": "この推計に与えた前提の基準日。data/context.json で管理している。",
        },
        # 手動項目を data より先に置く。Gemini の出力がこれらを上書きしないようにする
        **carried,
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

    ダーク船補正はここでは掛けない。補正は推計であり、
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
        "source": GEMINI_SOURCE,
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

    context = load_context()
    age = context_age_days(context)
    stale_after = context.get("stale_after_days")
    print(f"[Context] 前提の基準日: {context.get('context_updated')}（{age}日経過）")
    if age is not None and stale_after is not None and age > stale_after:
        print(
            f"[Context] WARNING: 前提が {stale_after} 日を超えて更新されていません。"
            "data/context.json を見直してください。",
            file=sys.stderr,
        )

    print("[Manual] Analyzing with Gemini...")
    data = analyze_with_gemini(api_key, news_items, context)

    if not data:
        print("[Manual] Gemini analysis failed. Skipping update.", file=sys.stderr)
        sys.exit(1)

    manual = build_manual_json(data, context, load_existing_manual())
    save_manual(manual)

    # AIS推計は実測とは別ファイル（data/ais-estimate.json）に保存する
    save_ais_estimate(data)

    print("[Manual] Done.")
    print(f"  シナリオA: {data['scenario']['A_diplomacy_pct']}%")
    print(f"  流量: {data['hormuz_daily_flow_mbpd']} MBPD")
    print(f"  状況メモ: {data.get('last_manual_note', '—')}")

if __name__ == "__main__":
    main()
