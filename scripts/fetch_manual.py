"""
fetch_manual.py
RSSフィードから最新ニュースを取得し、
Google Gemini API で分析して
data/manual-update.json を自動更新する。

必要環境変数:
  GEMINI_API_KEY : Google Gemini API キー
"""

import argparse
import html
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
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

# 入力に使う記事の条件。2026-10-25 までは先頭のフィードだけで上限の40件に達し、
# 残り8本は読まれていなかった。並びも関連度順で、中央値18日・最大102日前の
# 見出しが入っていた（10/25 の実測）。フィードごとに上限を設けて全フィードから取り、
# 期間で絞ってから新しい順に並べる。
MAX_AGE_DAYS = 7
PER_FEED_MAX = 12

# 媒体名を持たないフィード（<source> が無い）の表示名
FEED_LABELS = {
    "www.aljazeera.com": "Al Jazeera",
    "feeds.bbci.co.uk": "BBC",
    "rss.nytimes.com": "New York Times",
}


def parse_pub_date(raw):
    """RSS の pubDate を UTC の datetime にする。読めない場合は None。"""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_text(raw):
    """HTML のタグと実体参照を取り除き、空白を1つにまとめる。"""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def title_key(title):
    """重複判定用の見出し。Google ニュースが末尾に付ける「 - 媒体名」を除く。"""
    base = re.sub(r"\s+-\s+[^-]+$", "", title or "")
    return re.sub(r"\W+", "", base).lower()


def fetch_feed(url, now, max_age_days):
    """1本のフィードから、期間内でキーワードに合う記事を新しい順に返す。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        root = ET.parse(response).getroot()
    host = urllib.parse.urlparse(url).hostname or ""
    items = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title", ""))
        desc = clean_text(item.findtext("description", ""))
        published = parse_pub_date(item.findtext("pubDate", ""))
        # 日付の無い記事は新しさを確かめられないので使わない
        if not title or published is None:
            continue
        if (now - published) > timedelta(days=max_age_days):
            continue
        if not any(kw.lower() in f"{title} {desc}".lower() for kw in KEYWORDS):
            continue
        # Google ニュースの description は見出しとリンクの繰り返しで、中身が無い
        if title_key(desc).startswith(title_key(title)):
            desc = ""
        source = clean_text(item.findtext("source", "")) or FEED_LABELS.get(host, host)
        items.append({
            "title": title,
            "description": desc[:200],
            "source": source,
            "published": published,
        })
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def fetch_rss_news(max_items=40, max_age_days=MAX_AGE_DAYS, per_feed_max=PER_FEED_MAX):
    """全フィードからホルムズ関連の記事を集め、重複を除いて新しい順に返す。"""
    now = datetime.now(timezone.utc)
    collected = []
    for url in RSS_FEEDS:
        try:
            items = fetch_feed(url, now, max_age_days)
        except Exception as e:
            print(f"[RSS] Error fetching {url}: {e}", file=sys.stderr)
            continue
        print(f"[RSS] {len(items):3d} 件（期間内・該当）: {url[:70]}")
        collected.extend(items[:per_feed_max])

    seen = set()
    unique = []
    for item in sorted(collected, key=lambda x: x["published"], reverse=True):
        key = title_key(item["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    news = unique[:max_items]
    if news:
        ages = sorted((now - n["published"]).total_seconds() / 86400 for n in news)
        print(f"[RSS] 採用 {len(news)} 件（重複除去前 {len(collected)} 件）"
              f" / 古さ 中央値 {ages[len(ages) // 2]:.1f} 日・最大 {ages[-1]:.1f} 日")
    return news


def format_news_line(item):
    """プロンプトに入れる1行。日付と媒体を先に置き、新しさと出所を読めるようにする。"""
    date = item["published"].astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    body = f": {item['description']}" if item["description"] else ""
    return f"- [{date}・{item['source']}] {item['title']}{body}"

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


def timeline_latest_date(context):
    """timeline に登録された事実のうち最も新しい日付。読めなければ None。"""
    dates = []
    for item in context.get("timeline", []):
        try:
            dates.append(datetime.strptime(str(item.get("date", "")), "%Y-%m-%d").date())
        except ValueError:
            continue
    return max(dates) if dates else None


def timeline_age_days(context):
    """timeline の最新エントリからの経過日数。

    context_updated は前提値（流量・隻数・係数）を変えたときに更新するもので、
    timeline に事実を足さなくても新しいままになりうる。timeline は推計の
    現況認識そのものを与えるため、鮮度を別に測る。
    """
    latest = timeline_latest_date(context)
    if latest is None:
        return None
    jst = timezone(timedelta(hours=9))
    return (datetime.now(jst).date() - latest).days


def timeline_is_stale(context):
    """timeline が閾値を超えて古いか。判定できない場合は False。"""
    age = timeline_age_days(context)
    limit = context.get("timeline_stale_after_days")
    return age is not None and limit is not None and age > limit


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


def scenario_judgment_date(context):
    """シナリオの判定日（今日 JST から scenario_horizon_days 日後）。期間が無ければ None。"""
    days = context.get("scenario_horizon_days")
    if not isinstance(days, int) or days <= 0:
        return None
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).date() + timedelta(days=days)


def build_scenario_text(context):
    """シナリオ定義を、判定日・判定の順と「キー: 説明」の行に整形する。"""
    defs = context.get("scenario_definitions", {})
    lines = []
    judgment = scenario_judgment_date(context)
    if judgment is not None:
        lines.append(
            f"- 判定日：{judgment.isoformat()}（今日から{context['scenario_horizon_days']}日後）。"
            "各シナリオの確率は「判定日にそのシナリオに当たる確率」として答えること"
        )
    rule = context.get("scenario_rule")
    if rule:
        lines.append(f"- 判定の順：{rule}")
    for key, desc in defs.items():
        if key.startswith("_"):
            continue
        lines.append(f"- {key}: {desc}")
    return "\n".join(lines)


def qualifier(entry):
    """前提値に付ける但し書き。verified が真なら何も付けない。"""
    if isinstance(entry, dict) and entry.get("verified") is True:
        return ""
    return "（出典未確認の概数）"


def build_prompt(news_items, context):
    """Gemini へ渡すプロンプトを組み立てる。

    数値レンジの誘導と「封鎖中」という状態の断定は置かない。現況は
    【確定した経緯】と【最新ニュース】から読ませる。2026-09-05 以前は
    「封鎖中は2〜10程度」「8〜24隻が目安」「根拠がなければ5〜15」等の
    目安を与えており、同一入力の比較で推計隻数が実測 5 倍ぶん下振れして
    いたため撤去した。
    """
    normal_flow = context["normal_flow_mbpd"]["value"]
    normal_vessels = context.get("normal_vessels_per_day", {}).get("value")
    # 一次資料で裏づけた前提まで「出典未確認」と書かない。
    # 不当な但し書きは、その前提から離れた推計を誘発する。
    flow_note = qualifier(context.get("normal_flow_mbpd"))
    vessels_note = qualifier(context.get("normal_vessels_per_day"))
    # 見出しには経緯の最新日を出す。context_updated は前提値（流量・隻数・係数）の
    # 基準日で、経緯を足さなくても新しいままになるため、経緯の鮮度を誤認させる
    # （2026-09-20 修正。以前は「基準日 2026-09-05」と出る一方で中身の最新は 9/3 だった）。
    latest = timeline_latest_date(context)
    timeline_latest = latest.isoformat() if latest else "不明"
    timeline_text = build_timeline_text(context)
    scenario_text = build_scenario_text(context)

    news_text = "\n".join(format_news_line(item) for item in news_items)

    # 隻数の前提は表示側の「開戦前比」の分母と同じ値を使う。
    # 分子と分母で前提が違うと割合が読めなくなる。
    vessels_line = (
        f"- 通常時のホルムズ通過隻数は約{normal_vessels}隻/日{vessels_note}\n"
        if normal_vessels is not None else ""
    )

    return f"""
あなたはホルムズ海峡・イラン情勢の専門アナリストです。
以下の最新ニュースを分析して、JSON形式で回答してください。
必ずJSON形式のみで返答し、説明文は不要です。

【確定した経緯】（最新の事実 {timeline_latest} まで・data/context.json より）
{timeline_text}

【流量に関する前提】
- 通常時のホルムズ通過量は約{normal_flow}百万バレル/日{flow_note}
{vessels_line}- 現在の水準は上記の経緯と最新ニュースから判断すること。あらかじめ増減の方向を仮定しない

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
  "hormuz_daily_flow_mbpd": <ホルムズ通過量 百万バレル/日。最新ニュースの記述を根拠に推計する>,
  "hormuz_normal_flow_mbpd": {normal_flow},
  "flow_disruption_pct": <流量disruption率 整数 = round((1 - hormuz_daily_flow_mbpd / {normal_flow}) * 100)>,
  "last_manual_note": "<最新状況メモ 日本語100文字以内 ホルムズ封鎖・イラン情勢に関する最新動向>",
  "ais_estimated_vessels": <整数。対象は緯度25.8〜27.0°N・経度55.6〜57.0°Eのバウンディングボックス内を【通過中】の船舶のみ。待機中・引き返し中・停泊中は除外。最新ニュースの記述を根拠に推計する>,
  "ais_estimated_tankers": <整数。上記通過中船舶のうちタンカーのみ>,
  "ais_estimated_cargo": <整数。上記通過中船舶のうち貨物船のみ>,
  "ais_confidence": "<high / medium / low のいずれか1語。推計の信頼度>",
  "ais_estimation_note": <推計根拠を30文字以内で記載。信頼度の語は含めず根拠のみ>
}}

【シナリオの定義】（表示側のカード見出しと一致させてある。この定義と判定の順に沿って確率を割り当てること。
現状がどのシナリオに近いかではなく、判定日にどれに当たるかを見積もること）
{scenario_text}

【注意事項】
- シナリオ確率の合計は必ず100になること
- hormuz_daily_flow_mbpd と ais_estimated_vessels は、最新ニュースに現れる
  具体的な記述（通過隻数・通過量・通航状況の報道）を根拠に推計すること。
  報道が示す水準をそのまま反映し、目安の数値へ寄せないこと
- 通常時の値をそのまま返さず、現在の水準として推計した値を返すこと
- flow_disruption_pctはhormuz_daily_flow_mbpdから計算すること
- last_manual_noteはホルムズ・イラン情勢に関する内容のみ記載
- ais_estimated_tankers + ais_estimated_cargo <= ais_estimated_vessels
- ais_estimated_vesselsは「通過中」のみカウント。周辺待機・引き返し船は含めない
- 根拠となる報道が見つからない場合は、推測で数値を埋めずに
  ais_confidence を low とし、ais_estimation_note にその旨を書くこと
- ais_confidenceは必ず high / medium / low のいずれか1語のみ。文章にしない
- 直接の根拠となる報道がなく背景知識からの外挿にとどまる場合は low とすること
"""


class QuotaExhausted(Exception):
    """推計に使う API・サブスクの利用枠を使い切った。"""


def parse_json_text(text):
    """モデルの返答から JSON を取り出す（コードフェンスで囲まれていても読む）。"""
    text = (text or "").strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def with_retries(label, call):
    """call() を最大3回試す。利用枠切れは再試行せずに QuotaExhausted を投げる。"""
    for attempt in range(3):
        try:
            return call()
        except QuotaExhausted:
            raise
        except Exception as e:
            print(f"[{label}] Attempt {attempt+1} failed: {e}", file=sys.stderr)
            # Gemini の無料枠は1日20回（gemini-2.5-flash・2026-10-25 の 429 の応答）。
            # 使い切ったあとの再試行は枠を回復させず、待つ時間だけがかかる（10/25 の測定で約1時間）
            if "RESOURCE_EXHAUSTED" in str(e):
                raise QuotaExhausted(str(e)) from e
            if attempt < 2:
                wait = (attempt + 1) * 30  # 30秒→60秒待機
                print(f"[{label}] Waiting {wait}s before retry...", file=sys.stderr)
                time.sleep(wait)
    print(f"[{label}] All retries failed.", file=sys.stderr)
    return None


GEMINI_MODEL = "gemini-2.5-flash"


def analyze_with_gemini(api_key, news_items, context, prompt=None):
    """Gemini API でニュースを分析してJSONを生成（1回分）"""
    client = genai.Client(api_key=api_key)

    if prompt is None:
        prompt = build_prompt(news_items, context)

    def call():
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return parse_json_text(response.text)

    return with_retries("Gemini", call)


# Claude は Claude Code の CLI を、サブスクの認証（CLAUDE_CODE_OAUTH_TOKEN）で呼ぶ。
# ツールは使わせず（--tools ""）、プロンプトに入れた材料だけで答えさせる。
CLAUDE_MODEL = os.environ.get("SCENARIO_CLAUDE_MODEL", "opus")
CLAUDE_SYSTEM = "あなたは中東情勢と海上輸送の専門アナリストです。指示された JSON だけを返し、説明文やコードフェンスは書きません。"
CLAUDE_TIMEOUT_SEC = 600


def analyze_with_claude(prompt, model=CLAUDE_MODEL):
    """Claude（Claude Code の CLI）でニュースを分析してJSONを生成（1回分）"""
    cmd = ["claude", "-p", "--tools", "", "--model", model, "--output-format", "json",
           "--no-session-persistence", "--system-prompt", CLAUDE_SYSTEM]

    # 登録した値に改行が混じることがある（2026-10-25：setup-token の出力の折り返しごと
    # 貼られ、"Not logged in" になった）。トークンに空白は含まれないので取り除いて渡す
    token = re.sub(r"\s+", "", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=token)

    def call():
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", timeout=CLAUDE_TIMEOUT_SEC, env=env)
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout)[:300]}")
        if out.get("is_error"):
            msg = str(out.get("result") or out.get("subtype"))
            # 利用上限とログインの失敗は、再試行しても直らない
            if any(w in msg.lower() for w in ("limit", "not logged in", "authenticate")):
                raise QuotaExhausted(msg)
            raise RuntimeError(msg)
        return parse_json_text(out.get("result"))

    return with_retries("Claude", call)


# 推計に使うモデル。表示側の出典と ais-estimate.json の model はここから作る
BACKENDS = {
    "gemini": {"label": "Gemini 2.5 Flash", "model": GEMINI_MODEL},
    "claude": {"label": f"Claude（{CLAUDE_MODEL}）", "model": f"claude-{CLAUDE_MODEL}"},
}


def source_text(backend):
    """出典表示の文字列。1件だけ読んでも供給元・モデル・入力が分かる形にする。"""
    return (f"{BACKENDS[backend]['label']} による推計"
            "（入力: Google ニュース検索・BBC・Al Jazeera・New York Times の公開RSS）")


def make_estimator(backend):
    """backend の1回分の推計関数 prompt -> dict|None を返す。"""
    if backend == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise SystemExit("[Manual] ERROR: GEMINI_API_KEY not set.")
        return lambda prompt: analyze_with_gemini(api_key, None, None, prompt=prompt)
    if backend == "claude":
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise SystemExit("[Manual] ERROR: CLAUDE_CODE_OAUTH_TOKEN not set.")
        return analyze_with_claude
    raise SystemExit(f"[Manual] ERROR: unknown backend {backend}")


SCENARIO_KEYS = ("A_diplomacy_pct", "B_partial_blockade_pct", "C_full_blockade_pct", "D_escalation_pct")

# 1回の推計は乱数の影響が大きい。9/7〜10/25 の98回では、B が1回ごとに中央値で
# 20ポイント動いていた（同じ日の朝と昼でも同じ幅）。同じ入力で複数回答えさせ、
# 中央値を採る。値を出す最低の回数に満たないときは更新しない（前回の値を残す）。
SAMPLES = 5
MIN_SAMPLES = 3
SAMPLE_INTERVAL_SEC = 5


def round_to_total(values, total=100):
    """合計が total になるよう整数に丸める（最大剰余法）。"""
    s = sum(values)
    if s <= 0:
        return [0] * len(values)
    scaled = [v * total / s for v in values]
    floors = [int(x) for x in scaled]
    order = sorted(range(len(values)), key=lambda i: scaled[i] - floors[i], reverse=True)
    for i in order[: total - sum(floors)]:
        floors[i] += 1
    return floors


def sample_scenario(sample):
    """1回分の出力からシナリオ確率を取り出し、合計100に揃える。不正なら None。"""
    sc = sample.get("scenario") if isinstance(sample, dict) else None
    if not isinstance(sc, dict):
        return None
    try:
        values = [float(sc[k]) for k in SCENARIO_KEYS]
    except (KeyError, TypeError, ValueError):
        return None
    if any(v < 0 for v in values) or sum(values) <= 0:
        return None
    return round_to_total(values)


def collect_samples(estimate, news_items, context, n=SAMPLES):
    """同じプロンプトで n 回答えさせ、確率が読めた回だけ返す。estimate は make_estimator の戻り値。"""
    prompt = build_prompt(news_items, context)
    samples = []
    for i in range(n):
        if i:
            time.sleep(SAMPLE_INTERVAL_SEC)
        try:
            data = estimate(prompt)
        except QuotaExhausted:
            print(f"[Estimate] 回 {i + 1}: 利用枠を使い切ったため、残りの回は行いません", file=sys.stderr)
            break
        if data is None or sample_scenario(data) is None:
            print(f"[Estimate] 回 {i + 1}: 確率が読めないため除外", file=sys.stderr)
            continue
        samples.append(data)
        print(f"[Estimate] 回 {i + 1}: {dict(zip('ABCD', sample_scenario(data)))}")
    return samples


def median_number(samples, key, cast=float):
    vals = []
    for s in samples:
        try:
            vals.append(cast(s[key]))
        except (KeyError, TypeError, ValueError):
            continue
    return statistics.median(vals) if vals else None


def aggregate_samples(samples, context):
    """複数回の出力を1つにまとめる。

    数値は回ごとの中央値。文章（状況メモ・推計根拠）は、確率が中央値に
    いちばん近い回のものを使う（数値と文章の組み合わせを崩さないため）。
    """
    vectors = [sample_scenario(s) for s in samples]
    medians = [statistics.median(v[i] for v in vectors) for i in range(len(SCENARIO_KEYS))]
    scenario = round_to_total(medians)
    nearest = min(range(len(samples)),
                  key=lambda j: sum(abs(a - b) for a, b in zip(vectors[j], medians)))
    base = dict(samples[nearest])

    normal_flow = context["normal_flow_mbpd"]["value"]
    flow = median_number(samples, "hormuz_daily_flow_mbpd")
    if flow is not None:
        base["hormuz_daily_flow_mbpd"] = round(flow, 1)
        base["flow_disruption_pct"] = round((1 - flow / normal_flow) * 100)
    base["hormuz_normal_flow_mbpd"] = normal_flow

    vessels = median_number(samples, "ais_estimated_vessels", int)
    tankers = median_number(samples, "ais_estimated_tankers", int)
    cargo = median_number(samples, "ais_estimated_cargo", int)
    if vessels is not None:
        vessels = round(vessels)
        tankers = min(round(tankers or 0), vessels)
        cargo = min(round(cargo or 0), vessels - tankers)
        base.update(ais_estimated_vessels=vessels, ais_estimated_tankers=tankers,
                    ais_estimated_cargo=cargo)

    base["scenario"] = dict(zip(SCENARIO_KEYS, scenario))
    base["scenario_range"] = {
        k: [min(v[i] for v in vectors), max(v[i] for v in vectors)]
        for i, k in enumerate(SCENARIO_KEYS)
    }
    base["scenario_samples"] = len(samples)
    return base


def updated_today(path="data/manual-update.json"):
    """既存ファイルが今日（JST）すでに更新されているか。"""
    prev = load_existing_manual(path)
    raw = str(prev.get("updated_at") or "")
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    jst = timezone(timedelta(hours=9))
    return at.astimezone(jst).date() == datetime.now(jst).date()

def build_manual_json(data, context, previous=None, backend="gemini"):
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
    tl_latest = timeline_latest_date(context)
    prev = previous or {}
    carried = {k: prev.get(k, v) for k, v in MANUAL_ONLY_DEFAULTS.items()}
    return {
        "updated_at": now,
        "auto_generated": True,
        "source": source_text(backend),
        "context": {
            "updated": context.get("context_updated"),
            "age_days": age,
            "stale_after_days": stale_after,
            "stale": (age is not None and stale_after is not None and age > stale_after),
            "normal_flow_verified": context["normal_flow_mbpd"].get("verified", False),
            "timeline_updated": (
                tl_latest.isoformat() if tl_latest is not None else None
            ),
            "timeline_age_days": timeline_age_days(context),
            "timeline_stale_after_days": context.get("timeline_stale_after_days"),
            "timeline_stale": timeline_is_stale(context),
            "note": "この推計に与えた前提の基準日。data/context.json で管理している。",
        },
        # 確率が「いつの時点の話か」。表示側はこれで判定日を出す
        "scenario_horizon": {
            "days": context.get("scenario_horizon_days"),
            "judgment_date": (
                scenario_judgment_date(context).isoformat()
                if scenario_judgment_date(context) is not None else None
            ),
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


def save_ais_estimate(data, path="data/ais-estimate.json", backend="gemini"):
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
        "source": source_text(backend),
        "model": BACKENDS[backend]["model"],
        "method": f"公開RSS報道を {BACKENDS[backend]['label']} が分析した推計",
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

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-if-updated-today", action="store_true",
                   help="今日（JST）すでに更新済みなら何もしない（GitHub の予約実行＝予備の回で使う）")
    p.add_argument("--dry-run", action="store_true",
                   help="ファイルを書かず、結果を表示するだけ")
    p.add_argument("--backend", choices=sorted(BACKENDS),
                   default=os.environ.get("SCENARIO_BACKEND") or "gemini",
                   help="推計に使うモデル（既定は環境変数 SCENARIO_BACKEND、無ければ gemini）")
    p.add_argument("--samples", type=int, default=SAMPLES,
                   help=f"同じ入力で答えさせる回数（既定 {SAMPLES}）")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # 09:30 の cron-job.org の起動が本番、遅れて動く GitHub の予約実行は予備。
    # 予備の回が毎日上書きすると、昼に確率が入れ替わる（2026-10-25 まで）。
    if args.skip_if_updated_today and updated_today():
        print("[Manual] 今日はすでに更新済みのため、予備の実行は何もしません。")
        sys.exit(0)

    estimate = make_estimator(args.backend)

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

    tl_age = timeline_age_days(context)
    tl_limit = context.get("timeline_stale_after_days")
    tl_latest = timeline_latest_date(context)
    print(
        f"[Context] 経緯の最新: {tl_latest}"
        f"（{tl_age}日経過 / 閾値 {tl_limit}日）"
    )
    if timeline_is_stale(context):
        print(
            f"[Context] WARNING: 経緯（timeline）が {tl_limit} 日を超えて追記されていません。"
            "推計は古い現況認識のまま出力されます。data/context.json の timeline に"
            "直近の事実を追記してください。",
            file=sys.stderr,
        )

    print(f"[Manual] Analyzing with {BACKENDS[args.backend]['label']}（{args.samples} 回）...")
    samples = collect_samples(estimate, news_items, context, n=args.samples)
    need = min(MIN_SAMPLES, args.samples)
    if len(samples) < need:
        print(f"[Manual] 確率が読めた回が {len(samples)} 回で、{need} 回に足りません。"
              "更新しません（前回の値を残します）。", file=sys.stderr)
        sys.exit(1)

    data = aggregate_samples(samples, context)
    print(f"[Manual] 中央値: {data['scenario']} / 幅: {data['scenario_range']}")

    if args.dry_run:
        print("[Manual] --dry-run のため書き込みません。")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    manual = build_manual_json(data, context, load_existing_manual(), backend=args.backend)
    save_manual(manual)

    # AIS推計は実測とは別ファイル（data/ais-estimate.json）に保存する
    save_ais_estimate(data, backend=args.backend)

    print("[Manual] Done.")
    print(f"  シナリオA: {data['scenario']['A_diplomacy_pct']}%")
    print(f"  流量: {data['hormuz_daily_flow_mbpd']} MBPD")
    print(f"  状況メモ: {data.get('last_manual_note', '—')}")

if __name__ == "__main__":
    main()
