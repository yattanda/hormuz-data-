"""
check_consistency.py
ダッシュボードの値どうしの食い違いを調べ、WARN をワークフローの要約（$GITHUB_STEP_SUMMARY）に出す
（docs/data-sources-redesign.md §5-6）。WARN が出ても終了コードは 0 にし、ワークフローは止めない。

| 検査             | WARN の条件 |
|------------------|-------------|
| PortWatch の鮮度 | latest_date が14日より古い |
| PortWatch の0隻  | 直近7日がすべて 0 |
| 報道値と実測     | 報道の通航数が、PortWatch の7日平均を「見えない割合」の報道値で割り戻した値の 0.5〜2倍の外 |
| 状況メモの価格   | EIA の最新の WTI・Brent のどちらからも ±10% を超えて離れた「○ドル」 |
| シナリオの幅     | 5回の幅が 20pt を超えた（PR #18 の実測は最大7pt） |

ファイルは書かない（要約への追記を除く）。

  python scripts/check_consistency.py [--data-dir DIR] [--today YYYY-MM-DD]
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

PORTWATCH_STALE_DAYS = 14
ZERO_RUN_DAYS = 7
REPORTED_RATIO_RANGE = (0.5, 2.0)
NOTE_PRICE_TOLERANCE = 0.10  # fetch_manual.py の NOTE_PRICE_TOLERANCE と同じ
SCENARIO_SPREAD_MAX_PT = 20


def load(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8") as f:
        return json.load(f)


def check_portwatch_fresh(pw, today):
    latest = date.fromisoformat(pw["latest_date"])
    age = (today - latest).days
    if age > PORTWATCH_STALE_DAYS:
        return [f"PortWatch の最新日が {latest}（{age}日前）で、{PORTWATCH_STALE_DAYS}日より古い。"
                "update_portwatch.yml の実行と PortWatch の画面を確かめる"]
    return []


def check_portwatch_zero(pw, today):
    daily = sorted(pw.get("daily") or [], key=lambda r: r["date"])[-ZERO_RUN_DAYS:]
    if len(daily) == ZERO_RUN_DAYS and all(int(r.get("total") or 0) == 0 for r in daily):
        return [f"PortWatch の通航が {daily[0]['date']}〜{daily[-1]['date']} の{ZERO_RUN_DAYS}日すべて 0 隻。"
                "封鎖か取得・集計の不具合か、PortWatch の画面で確かめる"]
    return []


def untracked_share(manual, context):
    """「見えない割合」（%）と出どころ。その日の報道値があればそれを、無ければ context.json の値を使う。"""
    figs = [f for f in manual.get("reported_figures") or [] if f.get("metric") == "untracked_share_pct"]
    if figs:
        f = figs[0]
        return float(f["value"]), f"{f.get('source')}・{f.get('published')} の報道値"
    u = context.get("untracked_share_reported") or {}
    if isinstance(u.get("value"), (int, float)):
        return float(u["value"]), f"context.json（{u.get('source_name')}・{u.get('period_label')}）"
    return None, None


def check_reported_vs_measured(manual, pw, context, today):
    reported = [f for f in manual.get("reported_figures") or [] if f.get("metric") == "transits_per_day"]
    if not reported:
        return []
    share, share_src = untracked_share(manual, context)
    avg7 = (pw.get("window") or {}).get("total")
    if share is None or not 0 <= share < 100 or not isinstance(avg7, (int, float)):
        return [f"報道の通航数が {len(reported)} 件あるが、見えない割合か PortWatch の7日平均が無く、突き合わせられない"]
    implied = avg7 / (1 - share / 100)
    lo, hi = REPORTED_RATIO_RANGE
    warns = []
    for f in reported:
        ratio = float(f["value"]) / implied if implied > 0 else float("inf")
        if not lo <= ratio <= hi:
            warns.append(
                f"報道の通航数 {f['value']:g}隻/日（{f.get('source')}・{f.get('published')}）が、"
                f"PortWatch の7日平均 {avg7:g}隻/日を見えない割合 {share:g}%（{share_src}）で割り戻した "
                f"{implied:.1f}隻/日の {ratio:.2f}倍で、{lo:g}〜{hi:g}倍の外。引用：{f.get('quote')}")
    return warns


def prices_in_note(text):
    """文中の「○ドル」「○〜○ドル」の数（fetch_manual.py の prices_in_note と同じ）。"""
    prices = []
    for a, b in re.findall(r"(\d+(?:\.\d+)?)(?:\s*[〜~～\-–]\s*(\d+(?:\.\d+)?))?\s*ドル", unicodedata.normalize("NFKC", text)):
        prices.extend(float(x) for x in (a, b) if x)
    return prices


def check_note_prices(manual, eia, today):
    """fetch_manual.py が捨てた文（note_price_warnings）と、載せた文を今の EIA で見直した結果。"""
    warns = [f"状況メモの価格 {w['prices']} が EIA の {w['reference_prices']} から ±{NOTE_PRICE_TOLERANCE:.0%} を超えるため、"
             f"文を載せなかった：{w['text']}" for w in manual.get("note_price_warnings") or []]
    refs = [v for v in (eia.get("wti_price_usd"), eia.get("brent_price_usd")) if isinstance(v, (int, float)) and v > 0]
    for s in manual.get("note_sentences") or []:
        off = [p for p in prices_in_note(s.get("text") or "")
               if refs and all(abs(p - r) / r > NOTE_PRICE_TOLERANCE for r in refs)]
        if off:
            warns.append(f"載せた状況メモの価格 {off} が、今の EIA（{eia.get('series_week')} の週）の {refs} から "
                         f"±{NOTE_PRICE_TOLERANCE:.0%} を超えている：{s['text']}")
    return warns


def check_scenario_spread(manual, today):
    warns = []
    for key, (lo, hi) in (manual.get("scenario_range") or {}).items():
        if hi - lo > SCENARIO_SPREAD_MAX_PT:
            warns.append(f"シナリオ {key} の {manual.get('scenario_samples')} 回の幅が {lo}〜{hi}%（{hi - lo}pt）で、"
                         f"{SCENARIO_SPREAD_MAX_PT}pt を超えた。入力の記事が割れているか、推計が不安定")
    return warns


def run_checks(data_dir, today):
    """検査ごとに (名前, WARN のリスト, 検査できなかった理由) を返す。1つが壊れても残りは続ける。"""
    files = {}
    for key, name in (("pw", "portwatch-hormuz.json"), ("manual", "manual-update.json"),
                      ("context", "context.json"), ("eia", "eia-weekly.json")):
        try:
            files[key] = load(data_dir, name)
        except (OSError, json.JSONDecodeError) as e:
            files[key] = e
    checks = [
        ("PortWatch の鮮度", check_portwatch_fresh, ("pw",)),
        ("PortWatch の0隻", check_portwatch_zero, ("pw",)),
        ("報道値と実測", check_reported_vs_measured, ("manual", "pw", "context")),
        ("状況メモの価格", check_note_prices, ("manual", "eia")),
        ("シナリオの幅", check_scenario_spread, ("manual",)),
    ]
    results = []
    for name, fn, needs in checks:
        broken = [k for k in needs if isinstance(files[k], Exception)]
        if broken:
            results.append((name, [], f"ファイルを読めない：{files[broken[0]]}"))
            continue
        try:
            results.append((name, fn(*(files[k] for k in needs), today), None))
        except (KeyError, TypeError, ValueError) as e:
            results.append((name, [], f"{type(e).__name__}: {e}"))
    return results


def report(results, today):
    n_warn = sum(len(w) for _, w, _ in results)
    n_error = sum(1 for _, _, e in results if e)
    lines = [f"## 食い違いの検査（{today}・WARN {n_warn} 件" + (f"・検査できない {n_error} 件" if n_error else "") + "）", "",
             "| 検査 | 結果 |", "|---|---|"]
    for name, warns, error in results:
        status = f"検査できない（{error}）" if error else (f"WARN {len(warns)} 件" if warns else "OK")
        lines.append(f"| {name} | {status} |")
    details = [(name, w) for name, warns, _ in results for w in warns]
    if details:
        lines += ["", "### WARN", ""] + [f"- **{name}**：{w}" for name, w in details]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--today", type=date.fromisoformat,
                   default=datetime.now(timezone(timedelta(hours=9))).date(),
                   help="基準の日（既定は今日 JST）。試験用")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    results = run_checks(args.data_dir, args.today)
    text = report(results, args.today)
    print(text)
    # GitHub Actions の注釈。実行の画面の上に出る
    if os.environ.get("GITHUB_ACTIONS"):
        for name, warns, error in results:
            for w in warns + ([f"検査できない：{error}"] if error else []):
                print(f"::warning title={name}::{w}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
