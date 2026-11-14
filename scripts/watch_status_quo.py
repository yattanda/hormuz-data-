"""
watch_status_quo.py
AI の毎日の「今日判定したら当たるシナリオ」の見立て（scenario-log.jsonl の status_quo_ai）が、
運営者の記録した現状（scenario-outcomes.json）と2日続けて違ったときだけ、GitHub の Issue で知らせる
（docs/data-sources-redesign.md §5-4）。

- 記録の正解は運営者が決める。AI の見立ては採点にも記録にも使わない（AI の予想を AI の答えで採点しないため）
- 知らせるのは、直近2日の見立てがどちらも記録と違い、しかも同じシナリオを指しているときだけ。
  2日は暦の上で続いた日（昨日と今日）に限り、最新の行が今日の分でなければ立てない
  （推計が失敗・休止した日は行が無く、離れた2日や古い行で知らせないため。催促は別に回す）
- 同じ変化（X → Y）の Issue が開いているか、閉じてから DISMISS_DAYS 日以内なら、新しく立てない
  （運営者が「変化ではない」と閉じた翌日に、同じ Issue が立ち続けないように）
- 記録がまだ1行も無いあいだは Issue を立てず、要約に「現状の記録がありません」と出す
- Issue は運営者（ASSIGNEE）に割り当て、本文の先頭で言及する。メールだけでは見逃しうるため、
  GitHub Mobile のプッシュ通知（割り当て・言及）でも届くようにする（2026-10-26 運営者）
- 見張りの Issue が REMIND_AFTER_DAYS 日以上開いたままなら、催促のコメントを1回だけ足す
  （足したかはコメントの印で照合）。要約にも「未対応の Issue があります」と出す
- --dry-run では Issue もコメントも足さず、要約に「立てるはずだった」「催促するはずだった」と出す
- 失敗しても終了コードは 0（ワークフローは止めない）

  python scripts/watch_status_quo.py [--data-dir DIR] [--extra-entry PATH] [--dry-run]

--extra-entry は fetch_manual.py --dry-run --dry-run-entry PATH が書いた「足すはずだった1行」。
試し実行では予測の記録に書かないため、これを今日の行として読む。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from score_scenarios import SCENARIO_KEYS, load_outcomes, today_jst

LABEL = "scenario-status"
DISMISS_DAYS = 7
ASSIGNEE = "yattanda"
REMIND_AFTER_DAYS = 3
REMINDER_MARKER = f"<!-- {LABEL}-reminder -->"
JST = timezone(timedelta(hours=9))


def entry_date(entry):
    """記録の1行の日付（JST）。"""
    return datetime.fromisoformat(entry["at"]).astimezone(JST).date()


def load_entries(log_path, extra_entry_path=None):
    """予測の記録を日付ごとに最後の1行にして、日付順に返す。extra は同じ日の行を置き換える。"""
    by_date = {}
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    by_date[entry_date(row)] = row
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    if extra_entry_path:
        try:
            with open(extra_entry_path, encoding="utf-8") as f:
                row = json.load(f)
            by_date[entry_date(row)] = row
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[Watch] WARN: {extra_entry_path} を読めません（{e}）", file=sys.stderr)
    return [(d, by_date[d]) for d in sorted(by_date)]


def ai_view(entry):
    """その日の見立てのシナリオのキー。不定・記録なしは None。"""
    sq = entry.get("status_quo_ai")
    return sq.get("scenario") if isinstance(sq, dict) and sq.get("scenario") in SCENARIO_KEYS else None


def scenario_names(context_path):
    """キー → 「B（膠着継続・外交不透明化）」の形の短い名前。"""
    try:
        with open(context_path, encoding="utf-8") as f:
            defs = json.load(f).get("scenario_definitions") or {}
    except (OSError, json.JSONDecodeError):
        defs = {}
    return {k: f"{k[0]}（{str(defs.get(k) or k).split(' — ')[0]}）" for k in SCENARIO_KEYS}


def evaluate(entries, outcomes, today):
    """Issue を立てるかを決める。{"open": bool, "message": 要約の1文, "change": (X, Y) or None, "days": [...]}
    today は見張りの基準の日（JST）。最新の行がこの日でなければ立てない。"""
    result = {"open": False, "change": None, "days": []}
    if not outcomes:
        result["message"] = "現状の記録がありません（scenario-outcomes.json の outcomes が空）。Issue は立てません"
        return result
    if not entries:
        result["message"] = "見立ての記録がありません"
        return result
    days = [(d, e, ai_view(e), outcomes.get(d)) for d, e in entries[-2:]]
    result["days"] = days
    d, _, ai, rec = days[-1]
    if d != today:
        result["message"] = (f"{today} の見立ての記録がありません（最新は {d}。推計が失敗・休止した可能性）。"
                             "Issue は立てません")
    elif rec is None:
        result["message"] = f"{d} を含む現状の記録がありません（最新の期間の to が過去の日付か、まだ記録の前）"
    elif ai is None:
        result["message"] = f"{d} の見立ては不定（多数決で決まらない・または記録に見立てが無い）"
    elif ai == rec:
        result["message"] = f"{d} の見立て {ai} は記録と一致"
    elif (len(days) < 2 or days[0][0] != d - timedelta(days=1)  # 昨日の行が無い（日が飛んだ）
          or days[0][3] is None or days[0][2] is None or days[0][2] == days[0][3]):
        result["message"] = f"{d} の見立て {ai} が記録 {rec} と違う（1日目。続けば Issue を立てる）"
    elif days[0][2] != ai:
        result["message"] = (f"{days[0][0]}・{d} の見立て（{days[0][2]}・{ai}）がどちらも記録と違うが、"
                             "見立てが割れているため Issue は立てません")
    else:
        result.update(open=True, change=(rec, ai),
                      message=f"{days[0][0]}・{d} の見立てが2日続けて {ai} で、記録 {rec} と違う")
    return result


def marker(change):
    return f"<!-- {LABEL}: {change[0]}->{change[1]} -->"


def gh(*args, input_text=None):
    """gh を呼ぶ。テストではこの関数を差し替える。"""
    r = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def list_issues():
    """見張りのラベルの Issue（開いている・閉じた両方）。本文の印の無いものは除く。"""
    issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "all", "--limit", "100",
                           "--json", "number,state,body,createdAt,closedAt,url,comments") or "[]")
    return [i for i in issues if f"<!-- {LABEL}: " in (i.get("body") or "")]


def jst_day(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).date()


def existing_issue(change, today, issues):
    """同じ変化の Issue のうち、開いているもの・閉じて DISMISS_DAYS 日以内のものを返す。無ければ None。"""
    for i in issues:
        if marker(change) not in (i.get("body") or ""):
            continue
        if i.get("state") == "OPEN":
            return i
        closed = i.get("closedAt")
        if closed and (today - jst_day(closed)).days <= DISMISS_DAYS:
            return i
    return None


def remind(issues, today, dry_run):
    """REMIND_AFTER_DAYS 日以上開いている見張りの Issue に、催促のコメントを1回だけ足す。要約の行を返す。"""
    lines = []
    for i in issues:
        if i.get("state") != "OPEN":
            continue
        days = (today - jst_day(i["createdAt"])).days
        if days < REMIND_AFTER_DAYS:
            continue
        lines.append(f"- 未対応の Issue があります（{days}日）：{i.get('url')}")
        if any(REMINDER_MARKER in (c.get("body") or "") for c in i.get("comments") or []):
            lines.append("  - 催促は済み（同じ Issue への催促は1回まで）")
            continue
        body = (f"@{ASSIGNEE} この Issue が{days}日開いたままです。現状が変わったかを判断し、"
                "`data/scenario-outcomes.json` に記録するか、変化ではなければ閉じてください。"
                f"催促はこの1回だけです。\n\n{REMINDER_MARKER}")
        if dry_run:
            lines.append("  - 試し実行のため、催促のコメントは足しません（足すはずだった）")
            continue
        try:
            gh("issue", "comment", str(i["number"]), "--body-file", "-", input_text=body)
            lines.append(f"  - @{ASSIGNEE} に催促のコメントを足しました")
        except (RuntimeError, OSError) as e:
            lines.append(f"  - 催促のコメントを足せませんでした：{e}")
    return lines


def issue_text(result, names):
    x, y = result["change"]
    first = result["days"][0][0]
    title = f"現状のシナリオが {names[x][0]} → {names[y][0]} に変わった可能性（{first}〜{result['days'][-1][0]}）"
    lines = [
        f"@{ASSIGNEE} 現状の記録の見直しをお願いします。",
        "",
        f"AI の毎日の見立て（今日判定したら当たるシナリオ）が、記録した現状 **{names[x]}** と2日続けて違い、"
        f"どちらも **{names[y]}** を指しています。",
        "",
        "見立ては知らせるためだけに使い、記録にも採点にも使いません。変わったかどうかは運営者が決めてください。",
        "",
    ]
    for d, entry, ai, rec in result["days"]:
        sq = entry.get("status_quo_ai") or {}
        votes = "・".join(f"{k[0]} {n}" for k, n in (sq.get("votes") or {}).items())
        lines += [f"### {d} の見立て：{names.get(ai, ai)}（{votes}・数えた回 {sq.get('valid_samples')}）", ""]
        if sq.get("reason"):
            lines += [f"理由：{sq['reason']}", ""]
        for s in sq.get("sources") or []:
            link = f"[{s.get('title')}]({s['link']})" if s.get("link") else s.get("title")
            lines.append(f"- {s.get('source')}・{s.get('published')}：{link}")
        lines.append("")
    lines += [
        "### 記録の足し方（`data/scenario-outcomes.json`）",
        "",
        f"変わったと判断したら、今の期間（{names[x]}）に `\"to\": \"<変わる前の日>\"` を書き、"
        "変わった日を `from` にした期間を足します（`to` は省くと今日まで続く扱い）。",
        "",
        "```json",
        json.dumps({"from": "<変わった日>", "scenario": y, "basis": "<判定の理由>",
                    "sources": [{"name": "<媒体名>", "url": "<URL>", "published": "<日付>"}],
                    "recorded_at": "<記録した日>"}, ensure_ascii=False, indent=2),
        "```",
        "",
        f"変わっていないと判断したら、この Issue を閉じてください。同じ変化（{x[0]} → {y[0]}）は閉じてから"
        f"{DISMISS_DAYS}日間は知らせません。",
        "",
        marker(result["change"]),
    ]
    return title, "\n".join(lines)


def run(data_dir, extra_entry, dry_run, today):
    lines = ["## 現状の見張り（AI の見立てと記録した現状）", ""]
    try:
        outcomes = load_outcomes(os.path.join(data_dir, "scenario-outcomes.json"), today)
    except (OSError, json.JSONDecodeError, SystemExit) as e:
        lines.append(f"- 現状の記録を読めません：{e}")
        outcomes = None
    result = None
    if outcomes is not None:
        result = evaluate(load_entries(os.path.join(data_dir, "scenario-log.jsonl"), extra_entry), outcomes, today)
        lines.append(f"- {result['message']}")
    # 記録がまだ無いあいだは Issue が立たないので、gh を呼ばずに終える
    if not outcomes:
        return "\n".join(lines)
    try:
        issues = list_issues()
    except (RuntimeError, OSError, json.JSONDecodeError, ValueError) as e:
        lines.append(f"- Issue を調べられないため、立てず催促もしません：{e}")
        return "\n".join(lines)
    if result["open"]:
        names = scenario_names(os.path.join(data_dir, "context.json"))
        title, body = issue_text(result, names)
        found = existing_issue(result["change"], today, issues)
        if found:
            state = "開いている" if found.get("state") == "OPEN" else f"{DISMISS_DAYS}日以内に閉じた"
            lines.append(f"- 同じ変化の Issue が{state}ため、新しく立てません：{found.get('url')}")
        elif dry_run:
            lines.append(f"- 試し実行のため立てません。立てるはずだった Issue：{title}")
            print(body)
        else:
            try:
                gh("label", "create", LABEL, "--color", "D93F0B", "--force",
                   "--description", "現状のシナリオの見張り（scripts/watch_status_quo.py）")
                url = gh("issue", "create", "--title", title, "--label", LABEL, "--assignee", ASSIGNEE,
                         "--body-file", "-", input_text=body).strip()
                lines.append(f"- Issue を立てました（@{ASSIGNEE} に割り当て）：{url}")
            except (RuntimeError, OSError) as e:
                lines.append(f"- Issue を立てられませんでした：{e}")
    lines += remind(issues, today, dry_run)
    return "\n".join(lines)


def today_line(data_dir, extra_entry, today):
    """食い違いの検査の要約に出す1行（その日の見立てと記録の一致・不一致）。"""
    try:
        outcomes = load_outcomes(os.path.join(data_dir, "scenario-outcomes.json"), today)
    except (OSError, json.JSONDecodeError, SystemExit) as e:
        return f"現状の見立て：記録を読めません（{e}）"
    result = evaluate(load_entries(os.path.join(data_dir, "scenario-log.jsonl"), extra_entry), outcomes, today)
    return f"現状の見立て：{result['message']}"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--extra-entry", metavar="PATH")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--today", type=date.fromisoformat, default=None, help="基準の日（既定は今日 JST）。試験用")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    text = run(args.data_dir, args.extra_entry, args.dry_run, args.today or today_jst())
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
