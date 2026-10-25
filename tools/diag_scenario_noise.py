"""
diag_scenario_noise.py
シナリオ確率のばらつきを、同じ入力で繰り返し答えさせて測る（ファイルは書かない）。

ニュースは最初に1回だけ取得し、すべての回で同じものを使う。比べる形は「形:モデル」で指定する：
  legacy:<model> : 2026-10-25 までの入力（先頭のフィードから40件・日付で絞らない）で1回ずつ
  new1:<model>   : 新しい入力（全フィード・直近7日・新しい順）で1回ずつ
  newN:<model>   : 新しい入力で --samples 回答えさせた中央値（本番と同じ形）を --repeats 回
  <model> は gemini または claude。Gemini の無料枠は1日20回（再試行も数える）なので、回数に注意する

使い方（API キー・サブスクの認証が要るので Actions の diag_estat.yml を command=scenario_noise で動かす）:
  python tools/diag_scenario_noise.py --variants new1:gemini,new1:claude --runs 5
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


def single_runs(estimate, prompt, runs):
    out = []
    for i in range(runs):
        if i:
            time.sleep(fm.SAMPLE_INTERVAL_SEC)
        try:
            data = estimate(prompt)
        except fm.QuotaExhausted:
            print("  利用枠を使い切ったため中止")
            break
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
              f" 中央値 {statistics.median(vals)} 標準偏差 {statistics.pstdev(vals):.1f}")
    top = ["ABCD"[v.index(max(v))] for v in vectors]
    print(f"  幅の最大 {max(widths)} pt / 幅の平均 {statistics.mean(widths):.1f} pt"
          f" / 最も高いシナリオ: {''.join(top)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variants", default="new1:gemini,new1:claude")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--samples", type=int, default=fm.SAMPLES)
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args()

    context = fm.load_context()
    variants = [tuple(v.strip().split(":")) for v in args.variants.split(",") if v.strip()]
    estimators = {}

    def estimator(model):
        if model not in estimators:
            estimators[model] = fm.make_estimator(model)
        return estimators[model]

    legacy_news = legacy_fetch() if any(f == "legacy" for f, _ in variants) else None
    new_news = fm.fetch_rss_news() if any(f != "legacy" for f, _ in variants) else None
    new_prompt = fm.build_prompt(new_news, context) if new_news else None

    results = []
    for form, model in variants:
        label = f"{form}:{model}"
        print(f"[{label}]")
        if form == "legacy":
            vecs = single_runs(estimator(model), legacy_prompt(legacy_news, context), args.runs)
        elif form == "new1":
            vecs = single_runs(estimator(model), new_prompt, args.runs)
        elif form == "newN":
            vecs = []
            for r in range(args.repeats):
                samples = fm.collect_samples(estimator(model), new_news, context, n=args.samples)
                if len(samples) >= min(fm.MIN_SAMPLES, args.samples):
                    agg = fm.aggregate_samples(samples, context)
                    if agg is None:
                        print(f"  中央値 {r + 1}: 答えが割れて合計 0 のため出せず")
                        continue
                    vec = list(agg["scenario"].values())
                    print(f"  中央値 {r + 1}: {vec}")
                    vecs.append(vec)
        else:
            sys.exit(f"unknown form: {form}")
        results.append((label, vecs))

    for label, vectors in results:
        summarize(label, vectors)


if __name__ == "__main__":
    main()
