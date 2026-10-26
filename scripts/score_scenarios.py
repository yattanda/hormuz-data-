"""
score_scenarios.py
シナリオ確率の当たり具合を、多クラスの Brier スコアで採点する（docs/data-sources-redesign.md §5-4）。

  予測の記録  : data/scenario-log.jsonl（fetch_manual.py が推計のたびに1行足す）
  当たりの記録: data/scenario-outcomes.json（判定日が来たら運営者が手で足す）

Brier スコアは 4つのシナリオについて (確率 − 当たりなら1・外れなら0)² を足したもの。
0 が満点、2 が最悪。次の2つと比べる。
  - 一様      : 各25%（いつも 0.75）
  - 前の日のまま: 1つ前の記録の確率を、その日の予想としたもの
                  （毎日の見直しが当たり具合を良くしているか）

同じ判定日の記録が複数あるとき（手動で再実行した日など）は、最後の1行を使う。
ファイルは書かない。採点が貯まるまでは、確率の精度を表示で主張しない。

  python scripts/score_scenarios.py [--log PATH] [--outcomes PATH]
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

SCENARIO_KEYS = ("A_diplomacy_pct", "B_partial_blockade_pct", "C_full_blockade_pct", "D_escalation_pct")
UNIFORM = {k: 100 / len(SCENARIO_KEYS) for k in SCENARIO_KEYS}


def load_log(path):
    """予測の記録を読み、判定日ごとに最後の1行を残して日付順に返す。壊れた行は飛ばす。"""
    by_date = {}
    try:
        f = open(path, encoding="utf-8")
    except FileNotFoundError:
        return []
    with f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                judgment = date.fromisoformat(row["judgment_date"])
                probs = {k: float(row["scenario"][k]) for k in SCENARIO_KEYS}
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                print(f"[Score] WARN: {path} の {n} 行目を読めないため飛ばしました（{e}）", file=sys.stderr)
                continue
            by_date[judgment] = {"judgment_date": judgment, "at": row.get("at"), "scenario": probs}
    return [by_date[d] for d in sorted(by_date)]


def load_outcomes(path):
    """当たりの記録を {判定日: シナリオのキー} にする。期間が重なっていたら止める。"""
    with open(path, encoding="utf-8") as f:
        records = json.load(f).get("outcomes") or []
    result = {}
    for r in records:
        start = date.fromisoformat(r["from"])
        end = date.fromisoformat(r.get("to") or r["from"])
        if r["scenario"] not in SCENARIO_KEYS or end < start:
            raise SystemExit(f"[Score] ERROR: 当たりの記録が不正です: {r}")
        d = start
        while d <= end:
            if d in result:
                raise SystemExit(f"[Score] ERROR: {d} の当たりが2件あります（期間が重なっています）")
            result[d] = r["scenario"]
            d += timedelta(days=1)
    return result


def brier(probs, outcome):
    """多クラスの Brier スコア。probs は百分率（合計100）。"""
    total = sum(probs.values())
    return sum((probs[k] / total - (1.0 if k == outcome else 0.0)) ** 2 for k in SCENARIO_KEYS)


def score(log, outcomes):
    """判定日に当たりが記録されている予測を採点する。

    3つを同じ日の集まりで比べるため、1つ前の記録が無い最初の1行は採点に入れない。
    """
    rows = []
    for prev, cur in zip(log, log[1:]):
        outcome = outcomes.get(cur["judgment_date"])
        if outcome is None:
            continue
        rows.append({
            "judgment_date": cur["judgment_date"].isoformat(),
            "outcome": outcome,
            "model": brier(cur["scenario"], outcome),
            "uniform": brier(UNIFORM, outcome),
            "persistence": brier(prev["scenario"], outcome),
        })
    return rows


def summarize(rows):
    n = len(rows)
    if not n:
        return None
    mean = {k: sum(r[k] for r in rows) / n for k in ("model", "uniform", "persistence")}
    return {
        "n": n,
        "mean": mean,
        # 1 − 予測の Brier ÷ 比べる相手の Brier。正なら相手より良い
        "skill_vs_uniform": 1 - mean["model"] / mean["uniform"],
        "skill_vs_persistence": (1 - mean["model"] / mean["persistence"]) if mean["persistence"] else None,
    }


def report(log, outcomes, rows, summary):
    lines = ["## シナリオ確率の採点（多クラスの Brier スコア・0 が満点・2 が最悪）", ""]
    lines.append(f"- 予測の記録: {len(log)} 日分（判定日 "
                 + (f"{log[0]['judgment_date']}〜{log[-1]['judgment_date']}" if log else "なし") + "）")
    lines.append(f"- 当たりの記録: {len(outcomes)} 日分")
    if summary is None:
        lines.append("- 採点できる日がまだありません（最初の判定日は 2027-01-23）")
        return "\n".join(lines)
    m = summary["mean"]
    lines += [
        f"- 採点した日: {summary['n']} 日",
        "",
        "| 予想 | 平均 Brier | 予測の技能（1 − 予測 ÷ 相手） |",
        "|---|---|---|",
        f"| Claude の推計 | {m['model']:.3f} | — |",
        f"| 一様（各25%） | {m['uniform']:.3f} | {summary['skill_vs_uniform']:+.3f} |",
        f"| 前の日のまま | {m['persistence']:.3f} | "
        + (f"{summary['skill_vs_persistence']:+.3f}" if summary["skill_vs_persistence"] is not None else "—") + " |",
        "",
        "技能が正なら、その相手より当たっている。日々の予測は判定日が1日ずつずれるだけで中身が近く、"
        "互いに独立ではない。日数が少ないうちは差を読み過ぎない。",
    ]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", default="data/scenario-log.jsonl")
    p.add_argument("--outcomes", default="data/scenario-outcomes.json")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    log = load_log(args.log)
    outcomes = load_outcomes(args.outcomes)
    rows = score(log, outcomes)
    text = report(log, outcomes, rows, summarize(rows))
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")


if __name__ == "__main__":
    main()
