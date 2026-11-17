"""
check_design.py
ダッシュボード（index.html）の見た目が本体（chokepointlab.com）の決まりから外れていないかを調べる（課題46 D4・2026-11-17）。
外れていれば NG を出して終了コード 1（PR の検査を落とす）。

決まりの正本は本体 hormuz-map のスキル site-design と docs/assets/css/tokens.css。
検査の型は本体の tools/validate_daily.py の check_emoji()・check_colors()・check_selfhosted_assets() に合わせた
（正規表現は写し。本体で直したらここも直す）。

| 検査                 | NG の条件 |
|----------------------|-----------|
| 絵文字               | ファイルのどこか（表示される文・JS の文字列）に絵文字。残してよいのは ✕・★・✦ と矢印・▶◀◈ だけ |
| 直書きの色           | <style>・style=""・色の属性・JS の文字列に 16進・rgb()・hsl()・色名 |
| 区切りの飾り         | <style> と style="" に border-radius・linear/radial-gradient・box-shadow |
| 本体の CSS・書体     | tokens.css・BIZ UDPゴシック・IBM Plex Sans Condensed を https://chokepointlab.com/assets/ から読んでいない。Google Fonts への参照 |
| トークンの名前       | 使っている var(--c-*)・var(--f-*)・var(--map-*) が本体の tokens.css に無い（公開の tokens.css を取得して比べる。取れないときは WARN） |

ダッシュボードの JS は表示の文を組み立てるので、本体と違って JS の中の絵文字も見る。
oil-flow.json など過去のデータの絵文字は書き換えず、表示のときに外す（データは見ない）。

  python scripts/check_design.py [--file index.html] [--tokens-url URL | --tokens-file PATH]
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS_URL = "https://chokepointlab.com/assets/css/tokens.css"
ASSETS = "https://chokepointlab.com/assets/"
REQUIRED_LINKS = [
    ASSETS + "fonts/biz-udpgothic/biz-udpgothic.css",
    ASSETS + "fonts/ibm-plex-sans-condensed/ibm-plex-sans-condensed.css",
    ASSETS + "css/tokens.css",
]

# ── 本体の tools/validate_daily.py の写し（課題46 D3 PR 7b） ──
# 絵文字：矢印（←→↑↓ など）と ▶◀◈▲▼◆ のような図形の記号は絵文字に数えない
PAT_EMOJI_ANY = re.compile(r"[\U0001F000-\U0001FAFF☀-➿⌚⌛⏩-⏺⭐⭕⬛⬜\U0001F1E6-\U0001F1FF]")
EMOJI_ALLOWED = set("✕★✦")
PAT_COLOR = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3,4}\b|\b(?:rgba?|hsla?)\([^)]*\)")
PAT_COLOR_NAME = re.compile(r"\b(?:white|black|red|blue|green|yellow|orange|gray|grey|navy|silver|purple|pink)\b", re.I)
PAT_JS_COLOR = re.compile(r"""["'`][^"'`\n]*?((?:#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3,4}\b)(?![^\x00-\x7f])|(?:rgba?|hsla?)\([^)]*\))""")
PAT_JS_COLOR_NAME = re.compile(
    r"""(?:\.style\.\w+\s*=\s*|setProperty\(\s*['"][\w-]+['"]\s*,\s*|"""
    r"""\b(?:color|fillColor|background(?:-color|Color)?|fill|stroke|border(?:-\w+|Color)?|outline(?:-color|Color)?)\s*:\s*)"""
    r"""['"`]?\s*((?:white|black|red|blue|green|yellow|orange|gray|grey|navy|silver|purple|pink)\b)""", re.I)
# ── ここまで写し ──
PAT_DECOR = re.compile(r"\b(border-radius|(?:linear|radial)-gradient|box-shadow)\b")
PAT_TOKEN_USE = re.compile(r"var\(\s*(--(?:c|f|map)-[\w-]+)")
PAT_TOKEN_DEF = re.compile(r"(--[\w-]+)\s*:")

results = {"ng": 0, "warn": 0}


def ok(msg):
    print(f"OK   {msg}")


def warn(msg):
    results["warn"] += 1
    print(f"WARN {msg}")


def ng(msg):
    results["ng"] += 1
    print(f"NG   {msg}")


def css_hits(css):
    """(選択子, 色) の組。値の側だけを見る（クラス名 .st-danger などは色に数えない）"""
    out = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for sel, decls in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
        for val in re.findall(r":([^;{}]*)", decls):
            val = re.sub(r"var\([^)]*\)|url\([^)]*\)", "", val)
            out += [(sel.strip(), c) for c in PAT_COLOR.findall(val) + PAT_COLOR_NAME.findall(val)]
    return out


def split(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    styles = re.findall(r"<style\b[^>]*>(.*?)</style\b[^>]*>", text, flags=re.S | re.I)
    js = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script\b[^>]*>", text, flags=re.S | re.I))
    markup = re.sub(r"<script\b[^>]*>.*?</script\b[^>]*>|<style\b[^>]*>.*?</style\b[^>]*>", "", text, flags=re.S | re.I)
    return styles, js, markup


def check_emoji(text):
    # JS のコメントも含めてファイル全体を見る（コメントの絵文字は表示されないが、写して使われやすい）
    hits = [c for c in PAT_EMOJI_ANY.findall(re.sub(r"<!--.*?-->", "", text, flags=re.S)) if c not in EMOJI_ALLOWED]
    if hits:
        ng(f"絵文字が {len(hits)}個あります（{''.join(sorted(set(hits))[:8])}）。語で書く。"
           "データの絵文字は表示のときに外す（site-design「絵文字と記号」）")
    else:
        ok("絵文字なし（✕・★・✦・矢印を除く）")


def check_colors(styles, js, markup):
    bad = []
    for st in styles:
        bad += [f"<style> {sel[-30:]} {c}" for sel, c in css_hits(st)]
    for name, val in re.findall(r'\s(style|fill|stroke|color|stop-color|bgcolor)="([^"]*)"', markup):
        found = [c for _, c in css_hits("x{" + val + "}")] if name == "style" else PAT_COLOR.findall(val) + PAT_COLOR_NAME.findall(val)
        bad += [f'{name}="{c}"' for c in found]
    bad += [f"JS {c}" for c in PAT_JS_COLOR.findall(js) + PAT_JS_COLOR_NAME.findall(js)]
    if bad:
        ng(f"トークン外の色の直書きが {len(bad)}か所あります: {'／'.join(bad[:6])}。"
           "色は本体の tokens.css の変数（var(--c-*)）で書く（site-design「色」）")
    else:
        ok("トークン外の色の直書きなし（<style>・属性・JS）")


def check_decor(styles, js, markup):
    """<style>・style=""・JS のテンプレートの中の CSS（style="..."）を見る"""
    bad = []
    for st in styles:
        st = re.sub(r"/\*.*?\*/", "", st, flags=re.S)
        bad += [f"<style> {m}" for m in PAT_DECOR.findall(st)]
    for val in re.findall(r'\sstyle="([^"]*)"', markup + js):
        bad += [f'style="{m}"' for m in PAT_DECOR.findall(val)]
    if bad:
        ng(f"箱・角丸・影・グラデーションの区切りが {len(bad)}か所あります: {'／'.join(sorted(set(bad))[:6])}。"
           "罫（--c-rule）で区切る（site-design「区切り」）")
    else:
        ok("角丸・影・グラデーションなし")


def check_links(text):
    bad = []
    if re.search(r"fonts\.(?:googleapis|gstatic)\.com", text):
        bad.append("Google Fonts への参照があります")
    hrefs = re.findall(r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"', text)
    for need in REQUIRED_LINKS:
        if not any(h.split("?")[0] == need for h in hrefs):
            bad.append(f"<link> に {need} がありません")
    if bad:
        ng(f"本体の CSS・書体の読み込みが型と違います: {'・'.join(bad)}（D4 の Q7 a：本体の /assets/ を絶対 URL で読む）")
    else:
        ok("tokens.css・書体2つを本体の /assets/ から読み込み（Google Fonts への参照なし）")


def check_token_names(text, tokens_css, src):
    used = sorted(set(PAT_TOKEN_USE.findall(text)))
    if tokens_css is None:
        warn(f"本体の tokens.css を取得できず、トークンの名前を確かめられませんでした（{src}）。使っている {len(used)}個は未確認")
        return
    defined = set(PAT_TOKEN_DEF.findall(re.sub(r"/\*.*?\*/", "", tokens_css, flags=re.S)))
    missing = [t for t in used if t not in defined]
    if missing:
        ng(f"本体の tokens.css に無いトークンを使っています: {'・'.join(missing[:8])}（{src}）。"
           "本体で名前が変わったなら合わせて直し、<link> の ?v= も本体の各ページと同じ印に進める")
    else:
        ok(f"使っているトークン {len(used)}個はすべて本体の tokens.css にあります（{src}）")


def load_tokens(args):
    if args.tokens_file:
        try:
            return Path(args.tokens_file).read_text(encoding="utf-8"), args.tokens_file
        except OSError as e:
            return None, f"{args.tokens_file}: {e}"
    url = args.tokens_url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hormuz-data-check-design"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8"), url
    except Exception as e:  # 取得の失敗は WARN にとどめる（本体の配信が一時的に落ちても PR を止めない）
        return None, f"{url}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("--file", default=str(ROOT / "index.html"))
    ap.add_argument("--tokens-url", default=TOKENS_URL)
    ap.add_argument("--tokens-file")
    args = ap.parse_args()
    # Windows の手元（cp932）でも絵文字を含む行を出せるように
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    text = Path(args.file).read_text(encoding="utf-8")
    styles, js, markup = split(text)
    print(f"== {args.file}")
    check_emoji(text)
    check_colors(styles, js, markup)
    check_decor(styles, js, markup)
    check_links(text)
    tokens_css, src = load_tokens(args)
    check_token_names(text, tokens_css, src)
    print(f"== NG {results['ng']} / WARN {results['warn']}")
    return 1 if results["ng"] else 0


if __name__ == "__main__":
    sys.exit(main())
