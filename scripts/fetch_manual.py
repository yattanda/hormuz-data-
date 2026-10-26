"""
fetch_manual.py
RSSフィードから最新ニュースを取得し、
生成AI（既定は Claude・--backend で Gemini も選べる）で分析して
data/manual-update.json を自動更新する。

必要環境変数:
  CLAUDE_CODE_OAUTH_TOKEN : Claude のサブスクの認証（既定の claude のとき）
  GEMINI_API_KEY          : Google Gemini API キー（--backend gemini のとき）
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
import unicodedata
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
        # 媒体のドメイン。Google ニュースは <source url="https://www.reuters.com"> で元の媒体を示す。
        # 報道値の許可リスト（REPORTED_SOURCE_DOMAINS）の照合に使う
        source_el = item.find("source")
        source_url = source_el.get("url", "") if source_el is not None else ""
        items.append({
            "title": title,
            "description": desc[:200],
            "source": source,
            "domain": (urllib.parse.urlparse(source_url).hostname or host).lower(),
            "link": (item.findtext("link", "") or "").strip(),
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


def format_news_line(item, number):
    """プロンプトに入れる1行。番号・日付・媒体を先に置く。

    番号は reported_figures・note_sentences が根拠の記事を指すのに使う（1 始まり）。
    """
    date = jst_date(item["published"])
    body = f": {item['description']}" if item["description"] else ""
    return f"[{number}] [{date}・{item['source']}] {item['title']}{body}"


def jst_date(dt):
    return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

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


def build_prompt(news_items, context):
    """推計に渡すプロンプトを組み立てる。

    数値レンジの誘導と「封鎖中」という状態の断定は置かない。現況は
    【確定した経緯】と【最新ニュース】から読ませる。2026-09-05 以前は
    「封鎖中は2〜10程度」「8〜24隻が目安」「根拠がなければ5〜15」等の
    目安を与えており、同一入力の比較で推計隻数が実測 5 倍ぶん下振れして
    いたため撤去した。

    通航隻数と流量は 2026-10 の見直しで AI に推計させるのをやめた（IMF PortWatch で測る。
    docs/data-sources-redesign.md）。代わりに、記事に書かれた数字を原文の引用つきで
    抜き出させる（reported_figures）。抜き出した値は verify_reported_figures で確かめる。
    """
    # 見出しには経緯の最新日を出す。context_updated は前提値（流量・隻数・係数）の
    # 基準日で、経緯を足さなくても新しいままになるため、経緯の鮮度を誤認させる
    # （2026-09-20 修正。以前は「基準日 2026-09-05」と出る一方で中身の最新は 9/3 だった）。
    latest = timeline_latest_date(context)
    timeline_latest = latest.isoformat() if latest else "不明"
    timeline_text = build_timeline_text(context)
    scenario_text = build_scenario_text(context)

    news_text = "\n".join(format_news_line(item, i) for i, item in enumerate(news_items, 1))
    metrics_text = "\n".join(f"  - {k}: {v['desc']}（単位: {v['unit']}）" for k, v in REPORTED_METRICS.items())

    return f"""
あなたはホルムズ海峡・イラン情勢の専門アナリストです。
以下の最新ニュースを分析して、JSON形式で回答してください。
必ずJSON形式のみで返答し、説明文は不要です。

【確定した経緯】（最新の事実 {timeline_latest} まで・data/context.json より）
{timeline_text}

【最新ニュース】（{len(news_items)}件。先頭の [番号] が記事番号）
{news_text}

【出力形式】
{{
  "scenario": {{
    "A_diplomacy_pct": <0-100の整数>,
    "B_partial_blockade_pct": <0-100の整数>,
    "C_full_blockade_pct": <0-100の整数>,
    "D_escalation_pct": <0-100の整数>
  }},
  "reported_figures": [
    {{"metric": "<下の4種類のどれか>", "value": <数>, "quote": "<その数字を含む部分を、記事の見出しか説明文から一字一句そのまま>", "item": <記事番号>}}
  ],
  "note_sentences": [
    {{"text": "<最新状況メモの1文。日本語>", "items": [<その文の根拠の記事番号>]}}
  ]
}}

【シナリオの定義】（表示側のカード見出しと一致させてある。この定義と判定の順に沿って確率を割り当てること。
現状がどのシナリオに近いかではなく、判定日にどれに当たるかを見積もること）
{scenario_text}

【reported_figures の種類】
{metrics_text}

【注意事項】
- シナリオ確率の合計は必ず100になること
- reported_figures には、最新ニュースの見出しか説明文に数字として書かれているものだけを入れる。
  推計・換算・外挿・背景知識による補完はしない。該当が無ければ空の配列 [] にする
- quote は原文のまま（英語なら英語のまま）。翻訳・要約・言い換えをしない。省略記号も入れない
- value は quote の中の数字と同じ値にする。flow_mbpd だけは百万バレル/日に直してよい（例：800,000 barrels → 0.8）
- ホルムズ海峡（とその周辺の湾岸の航路）についての数字だけを入れる。他の海峡・世界全体・イランの輸出量などは入れない
- note_sentences はホルムズ・イラン情勢の最新動向を、合計100文字以内・1〜3文で書く
- note_sentences の各文には、その内容が書かれている記事の番号を items に必ず入れる。
  記事に書かれていない内容（【確定した経緯】だけにある事実・推測・背景知識）は書かない
"""


# reported_figures の種類。プロンプトの説明と、表示側の単位の正本。
REPORTED_METRICS = {
    "flow_mbpd": {"desc": "ホルムズ海峡を通る石油の量", "unit": "百万バレル/日"},
    "transits_per_day": {"desc": "ホルムズ海峡を1日に通航する船の数", "unit": "隻/日"},
    "untracked_share_pct": {"desc": "ホルムズ周辺で AIS を切っている・追跡できない船の割合", "unit": "%"},
    "war_risk_premium_pct": {"desc": "ホルムズ・湾岸向けの戦争リスク保険料率（船価に対する割合）", "unit": "%"},
}
MAX_REPORTED_FIGURES = 10

# 報道値として採る媒体（ドメイン → 表示名）。運営者が 2026-10-26 に決めた。
# 引用が記事にあることはコードで確かめられるが、媒体の信頼性は確かめられないため、名の通った
# 報道機関・海運専門紙に限る。Google ニュースの検索結果には出所のはっきりしないサイトも入る。
# サブドメイン（www.・uk. など）も含めて照合する。足すときは docs/data-sources-redesign.md §5-2 にも書く。
REPORTED_SOURCE_DOMAINS = {
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "lloydslist.com": "Lloyd's List",
    "lloydslistintelligence.com": "Lloyd's List Intelligence",
    "aljazeera.com": "Al Jazeera",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "ft.com": "Financial Times",
    "nytimes.com": "New York Times",
    "apnews.com": "AP",
}


def allowed_source(item):
    """記事の媒体が許可リストにあるか。"""
    domain = (item.get("domain") or "").lower()
    return any(domain == d or domain.endswith("." + d) for d in REPORTED_SOURCE_DOMAINS)


def normalize_text(text):
    """引用の照合用。全角半角・空白・引用符・大文字小文字の違いを吸収する。"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'}))
    return re.sub(r"\s+", " ", text).strip().lower()


def numbers_in(text):
    """文字列に現れる数を返す（3桁区切りのカンマは外す）。"""
    nums = []
    for raw in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", unicodedata.normalize("NFKC", str(text or ""))):
        try:
            nums.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return nums


def value_in_quote(value, quote, metric):
    """value が quote の中の数と一致するか。flow_mbpd は千・百万の位取りの違いを許す。"""
    scales = (1, 1e3, 1e6) if metric == "flow_mbpd" else (1,)
    for n in numbers_in(quote):
        for k in scales:
            target = value * k
            if abs(n - target) <= 1e-6 * max(1.0, abs(target)):
                return True
    return False


def article_ref(item):
    """表示と記録に使う記事の情報。"""
    return {
        "source": item["source"],
        "domain": item.get("domain") or None,
        "title": item["title"],
        "link": item.get("link") or None,
        "published": jst_date(item["published"]),
    }


def verify_reported_figures(samples, news_items):
    """全回の reported_figures を集め、コードで確かめて通ったものだけを返す。

    確かめること（docs/data-sources-redesign.md §5-2）：
    - metric が決めた種類のどれか・value が数である
    - item が入力記事の番号の範囲内にある
    - quote がその記事の見出しか説明文にそのまま含まれる
    - value の数字が quote の中にある
    - 記事の媒体が許可リスト（REPORTED_SOURCE_DOMAINS）にある
    引用が原文にあることは確かめられるが、数字の意味（何の量か）が metric と合っているかは
    確かめられない。表示側では引用文もあわせて出す。
    """
    found, dropped, off_list = {}, 0, set()
    for sample in samples:
        figs = sample.get("reported_figures") if isinstance(sample, dict) else None
        for f in figs if isinstance(figs, list) else []:
            try:
                metric = f["metric"]
                value = float(f["value"])
                quote = str(f["quote"]).strip()
                number = int(f["item"])
            except (KeyError, TypeError, ValueError):
                dropped += 1
                continue
            if metric not in REPORTED_METRICS or not quote or not 1 <= number <= len(news_items):
                dropped += 1
                continue
            item = news_items[number - 1]
            if not allowed_source(item):
                off_list.add(item.get("domain") or item["source"])
                dropped += 1
                continue
            haystacks = (normalize_text(item["title"]), normalize_text(item["description"]))
            if not any(normalize_text(quote) in h for h in haystacks) or not value_in_quote(value, quote, metric):
                dropped += 1
                continue
            key = (metric, value, number)
            if key not in found:
                found[key] = {"metric": metric, "value": value, "unit": REPORTED_METRICS[metric]["unit"],
                              "quote": quote, **article_ref(item)}
    figures = sorted(found.values(), key=lambda x: x["published"], reverse=True)[:MAX_REPORTED_FIGURES]
    print(f"[Reported] 採用 {len(figures)} 件・不採用 {dropped} 件（{len(samples)} 回分）")
    if off_list:
        print(f"[Reported] 許可リストに無い媒体のため捨てた: {', '.join(sorted(off_list))}")
    return figures


NOTE_MAX_CHARS = 100
# 状況メモの価格の検査で許す EIA の週次価格との差（±10%）。EIA は週次なので急変した週は外れうる。
NOTE_PRICE_TOLERANCE = 0.10


def load_reference_prices(path="data/eia-weekly.json"):
    """状況メモの価格の検査に使う EIA の最新の WTI・Brent。読めなければ空。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [v for v in (d.get("wti_price_usd"), d.get("brent_price_usd")) if isinstance(v, (int, float)) and v > 0]


def prices_in_note(text):
    """文中の「○ドル」「○〜○ドル」の数。「○万ドル」などの金額は拾わない。"""
    prices = []
    for a, b in re.findall(r"(\d+(?:\.\d+)?)(?:\s*[〜~～\-–]\s*(\d+(?:\.\d+)?))?\s*ドル", unicodedata.normalize("NFKC", text)):
        prices.extend(float(x) for x in (a, b) if x)
    return prices


def verify_note_sentences(sample, news_items, reference_prices):
    """状況メモの文を確かめ、通った文だけを根拠の記事つきで返す（docs/data-sources-redesign.md §5-3）。

    - items が無い文・範囲外の番号を含む文は捨てる
    - 「○ドル」が EIA の最新の WTI・Brent のどちらからも ±10% を超えて離れていたら、その文を捨てて WARN
    - 合計 NOTE_MAX_CHARS 字を超える分の文は捨てる
    """
    raw = sample.get("note_sentences") if isinstance(sample, dict) else None
    kept, total = [], 0
    for s in raw if isinstance(raw, list) else []:
        text = str(s.get("text") or "").strip() if isinstance(s, dict) else ""
        items = s.get("items") if isinstance(s, dict) else None
        if not text or not isinstance(items, list) or not items:
            print(f"[Note] 根拠の記事番号が無い文を捨てました: {text[:40]}", file=sys.stderr)
            continue
        try:
            numbers = sorted({int(n) for n in items})
        except (TypeError, ValueError):
            numbers = [0]
        if not all(1 <= n <= len(news_items) for n in numbers):
            print(f"[Note] 範囲外の記事番号を含む文を捨てました: {text[:40]}", file=sys.stderr)
            continue
        off = [p for p in prices_in_note(text)
               if reference_prices and all(abs(p - r) / r > NOTE_PRICE_TOLERANCE for r in reference_prices)]
        if off:
            print(f"[Note] WARN: 価格 {off} が EIA の {reference_prices} から ±{NOTE_PRICE_TOLERANCE:.0%} を超えるため文を捨てました: {text[:40]}",
                  file=sys.stderr)
            continue
        if total + len(text) > NOTE_MAX_CHARS:
            continue
        total += len(text)
        kept.append({"text": text, "items": numbers,
                     "sources": [article_ref(news_items[n - 1]) for n in numbers]})
    return kept


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


# 推計に使うモデル。表示側の出典と ais-estimate.json の model はここから作る。
# 2026-10-25 に Gemini から Claude（opus）へ切り替えた。同じ入力で1回ずつ5回答えさせた幅が
# Gemini は最大60ポイント、Claude は最大7ポイントだった（tools/diag_scenario_noise.py）。
# Claude が失敗した日は Gemini に切り替えず、更新しない（振れの大きい値を混ぜないため）
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


def nearest_sample(samples):
    """確率が中央値にいちばん近い回の番号。文章（状況メモ）はこの回のものを使う。"""
    vectors = [sample_scenario(s) for s in samples]
    medians = [statistics.median(v[i] for v in vectors) for i in range(len(SCENARIO_KEYS))]
    return min(range(len(samples)),
               key=lambda j: sum(abs(a - b) for a, b in zip(vectors[j], medians)))


def aggregate_samples(samples, context):
    """複数回の出力からシナリオ確率をまとめる（回ごとの中央値・幅・回数）。

    文章と報道値はここでは扱わない（verify_note_sentences・verify_reported_figures が
    入力記事と照らして確かめてから足す）。モデルの出力をそのまま載せないため、
    返す dict はここで決めたキーだけにする。
    """
    vectors = [sample_scenario(s) for s in samples]
    medians = [statistics.median(v[i] for v in vectors) for i in range(len(SCENARIO_KEYS))]
    # 回ごとに別のシナリオへ寄せた答えが並ぶと、中央値がすべて 0 になりうる
    # （例：A 100%・A 100%・B 100%・B 100%・C 100%）。確率として成り立たないので出さない
    if sum(medians) <= 0:
        return None
    return {
        "scenario": dict(zip(SCENARIO_KEYS, round_to_total(medians))),
        "scenario_range": {
            k: [min(v[i] for v in vectors), max(v[i] for v in vectors)]
            for i, k in enumerate(SCENARIO_KEYS)
        },
        "scenario_samples": len(samples),
    }


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
        # 手動項目を data より先に置く。推計の出力がこれらを上書きしないようにする
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-if-updated-today", action="store_true",
                   help="今日（JST）すでに更新済みなら何もしない（GitHub の予約実行＝予備の回で使う）")
    p.add_argument("--dry-run", action="store_true",
                   help="ファイルを書かず、結果を表示するだけ")
    p.add_argument("--backend", choices=sorted(BACKENDS),
                   default=os.environ.get("SCENARIO_BACKEND") or "claude",
                   help="推計に使うモデル（既定は環境変数 SCENARIO_BACKEND、無ければ claude）")
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
    if data is None:
        print("[Manual] 回ごとの答えが割れ、中央値の合計が 0 になりました。更新しません（前回の値を残します）。",
              file=sys.stderr)
        sys.exit(1)
    print(f"[Manual] 中央値: {data['scenario']} / 幅: {data['scenario_range']}")

    data["reported_figures"] = verify_reported_figures(samples, news_items)
    sentences = verify_note_sentences(samples[nearest_sample(samples)], news_items, load_reference_prices())
    data["note_sentences"] = sentences
    # 互換のため、確かめた文をつないだものを今までどおり持つ（どこにも表示していない。2026-10-26 確認）
    data["last_manual_note"] = "".join(s["text"] for s in sentences)

    if args.dry_run:
        print("[Manual] --dry-run のため書き込みません。")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    manual = build_manual_json(data, context, load_existing_manual(), backend=args.backend)
    save_manual(manual)

    # 隻数の AI 推計（data/ais-estimate.json）は 2026-10 の見直しで書かなくなった。
    # 通航数は IMF PortWatch で測る（scripts/fetch_portwatch.py）。ファイルは記録として残す

    print("[Manual] Done.")
    print(f"  シナリオA: {data['scenario']['A_diplomacy_pct']}%")
    print(f"  報道値: {len(data['reported_figures'])} 件")
    print(f"  状況メモ: {data['last_manual_note'] or '—'}")

if __name__ == "__main__":
    main()
