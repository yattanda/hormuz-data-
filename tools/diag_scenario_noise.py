"""
diag_scenario_noise.py
シナリオ確率のばらつきを、同じ入力で繰り返し答えさせて測る（ファイルは書かない）。

ニュースは最初に1回だけ取得し、すべての回で同じものを使う。比べる形：
  legacy : 2026-10-25 までの入力（先頭のフィードから40件・日付で絞らない）で1回ずつ
  new1   : 新しい入力（全フィード・直近7日・新しい順）で1回ずつ
  newN   : 新しい入力で --samples 回答えさせた中央値（本番と同じ形）を --repeats 回

使い方（GEMINI_API_KEY が要るので Actions の diag_scenario.yml から動かす）:
  python tools/diag_scenario_noise.py --variants legacy,new1,newN --runs 5 --samples 5 --repeats 3
"""

import argparse
import os
import statistics
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fetch_manual as fm  # noqa: E402

KEYS = fm.SCENARIO_KEYS


def legacy_fetch(max_items=40):
    """2026-10-25 までの fetch_rss_news と同じ取り方（比較用に残す）。"""
    items = []
    for url in fm.RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                root = ET.parse(response).getroot()
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    desc = item.findtext("description", "")
                    if any(kw.lower() in f"{title} {desc}".lower() for kw in fm.KEYWORDS):
                        items.append({"title": title, "description": desc[:200]})
            if len(items) >= max_items:
                break
        except Exception as e:
            print(f"[legacy RSS] Error fetching {url}: {e}", file=sys.stderr)
    return items[:max_items]


def legacy_prompt(news, context):
    original = fm.format_news_line
    fm.format_news_line = lambda item: f"- {item['title']}: {item['description']}"
    try:
        return fm.build_prompt(news, context)
    finally:
        fm.format_news_line = original


def single_runs(api_key, prompt, context, runs):
    out = []
    for i in range(runs):
        if i:
            time.sleep(fm.SAMPLE_INTERVAL_SEC)
        data = fm.analyze_with_gemini(api_key, None, context, prompt=prompt)
        vec = fm.sample_scenario(data) if data else None
        print(f"  回 {i + 1}: {vec}")
        if vec:
            out.append(vec)
    return out


def summarize(label, vectors):
    print(f"\n== {label}（{len(vectors)} 回）")
    if len(vectors) < 2:
        print("  回数が足りず、ばらつきを出せません")
        return
    widths = []
    for i, k in enumerate(KEYS):
        vals = [v[i] for v in vectors]
        widths.append(max(vals) - min(vals))
        print(f"  {k[0]}: 範囲 {min(vals)}〜{max(vals)}（幅 {widths[-1]}）"
              f" 標準偏差 {statistics.pstdev(vals):.1f}")
    top = ["ABCD"[v.index(max(v))] for v in vectors]
    print(f"  幅の最大 {max(widths)} pt / 最も高いシナリオ: {''.join(top)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variants", default="legacy,new1,newN")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--samples", type=int, default=fm.SAMPLES)
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY not set")
    context = fm.load_context()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    results = {}
    if "legacy" in variants:
        news = legacy_fetch()
        print(f"[legacy] {len(news)} 件")
        results["legacy（旧入力・1回ずつ）"] = single_runs(
            api_key, legacy_prompt(news, context), context, args.runs)

    if "new1" in variants or "newN" in variants:
        news = fm.fetch_rss_news()
        prompt = fm.build_prompt(news, context)
        if "new1" in variants:
            print("[new1]")
            results["new1（新入力・1回ずつ）"] = single_runs(api_key, prompt, context, args.runs)
        if "newN" in variants:
            print(f"[newN] {args.samples} 回の中央値 × {args.repeats}")
            agg = []
            for r in range(args.repeats):
                samples = fm.collect_samples(api_key, news, context, n=args.samples)
                if len(samples) >= min(fm.MIN_SAMPLES, args.samples):
                    vec = list(fm.aggregate_samples(samples, context)["scenario"].values())
                    print(f"  中央値 {r + 1}: {vec}")
                    agg.append(vec)
            results[f"newN（新入力・{args.samples} 回の中央値）"] = agg

    for label, vectors in results.items():
        summarize(label, vectors)


if __name__ == "__main__":
    main()
