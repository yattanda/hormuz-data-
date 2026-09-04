# VesselAPI 利用規約の照会記録

VesselAPI の無料枠で取得した集計値を、公開サイトに表示してよいかの確認記録。
**回答が届いたら本ファイルに追記すること。**

---

## 送信済み（2026-09-03）

| 項目 | 内容 |
|---|---|
| 送信日 | 2026-09-03 |
| 宛先 | `sales@vesselapi.com` |
| 件名 | Free tier: permission to display aggregate vessel counts on a public website |
| 差出人アカウント | `chokepointlab@gmail.com` |
| 署名形式 | 名 ＋ 姓のイニシャル（後置）／ 媒体名 ／ サイトURL ／ アカウントのメールアドレスを併記 |
| 状態 | **回答待ち** |

### 送付した文面

```
Hello,

I run a small Japanese-language news and open-source-intelligence site that
tracks the situation around the Strait of Hormuz:
https://yattanda.github.io/hormuz-map/

I am currently on the Free plan and would like to confirm what I am permitted
to publish before I build anything on top of your API.

How I intend to use the API
- Endpoint: GET /v1/location/vessels/bounding-box
- Area: the Strait of Hormuz (lat 25.8-27.0 / lon 55.6-57.0)
- Frequency: once per day, automated
- Measured usage: 3 calls per retrieval, about 90 calls per month

What I intend to publish
Only aggregate figures derived from the response, updated once a day:
- total number of unique vessels in the area
- number under way (SOG >= 1 knot)
- number stationary or drifting (SOG < 1 knot)

I do not intend to publish individual vessel data - no vessel names, MMSI or
IMO numbers, and no positions or tracks. Nothing that would let a reader
reconstruct the underlying data or use my page as a substitute for your service.

My questions
1. Is publishing these aggregate figures on a public website permitted under
   the Free plan?
2. Do you require attribution? If so, please tell me the exact wording and
   placement you expect, and I will follow it. I also plan to move the site to
   its own domain in the future - same operator, same site, same use of your
   data. Could you confirm that any permission you grant applies to the use I
   have described rather than to the current URL?
3. The site is not currently monetised, but I may introduce advertising in the
   future. Would that change the answer to question 1, and if so, which plan
   would I need?
4. API keys appear to expire after 90 days at most. Is there a way to renew or
   rotate a key without downtime, and do you send a notification before a key
   expires? The retrieval is automated, so a silent expiry would stop it
   without my noticing.

I would rather ask first than assume. Thank you for your time.
```

---

## 文面をこう書いた理由

### 「集計値のみ・個船情報なし」を先回りして明示した

商用 AIS プロバイダの規約には、**生データを as-is で公開表示することを禁じ、
集計・可視化などの付加価値プロダクトは認める**という条項が置かれるのが通例
（Datalastic の規約に実在する。README の「ライセンス上の注意」参照）。

そこで「個船情報は一切出さない」「読者が元データを復元できない」
「貴社サービスの代替にならない」の3点を自分から書いた。
仮に VesselAPI に同種の条項があっても、許諾の範囲に入る可能性を上げるため。

### 将来の収益化を自分から申告した（質問3）

伏せたまま後で広告を入れると、遡って規約違反になりかねない。
現時点で明かしておくほうが安全と判断した。

### 許諾を「URL」ではなく「用途」に紐づけるよう求めた（質問2）

独自ドメインへの移行予定があるため。
相手が善意で「その URL での表示を許可する」と回答すると、移行後に許諾の範囲が
曖昧になり再照会が必要になる。**移行先ドメイン名は未確定のため書いていない**
（未確定のものを書くと、そちらに許諾が固定されかねない）。

### キーの失効を質問に含めた（質問4）

API キーは最長90日で失効する。自動取得が黙って止まると、
**2026-04〜09 に「0隻」を4.5か月書き続けた事故と同型**になる。
提供元に通知の有無を確認しておく。

### 署名を「名＋姓のイニシャル」にした

許諾の実効性を担保しているのは名前ではなく、**アカウントのメールアドレス**と
**メールスレッドの記録**、そしてサイト URL。
このメールは新規に身元を名乗る場面ではなく、既に規約に同意済みのアカウント保有者に
よる用途確認であり、契約主体の特定は登録時点で済んでいる。

したがって姓をイニシャルにしても許諾の効力は実質的に落ちない。
ただし**今後すべての場面で同じ表記を使い続けること**。
表記がブレると同一人物性を追えなくなり、フルネームでないことより不利に働く。

有償契約・請求書の発行・法人契約に進む場合は正式名称の提示が要るが、
その時点で出せば足りる。

---

## 回答が来なかった場合の扱い（送信前に決定）

無料枠ユーザーからの `sales@` 宛て照会は、返信されない可能性が相応にある。

**2026-09-17（送信から2週間）までに返信がなければ、利用規約を自分で読んで
判断し、その根拠を本ファイルに記録したうえで公開に踏み切る。**

無期限に待つと依頼2（実測と推計の分離）の実装も止まったままになる。
また、集計3値の公開は多くの規約で許容範囲に入る書き方をしている。

※ これは法的助言ではない。最終判断は運営者が行う。

代替として検討したが採らなかった案:

- 回答が来るまで実測値の公開を保留し、推計のみの運用を続ける
  → 実装が止まる期間が読めない
- 集計値の粒度をさらに落とす（隻数を出さず「前日比 +12%」等の相対値のみ）
  → 読者にとっての情報価値が大きく下がる。回答次第では不要な妥協になる

---

## 回答記録

### 2026-09-04 受信（Bence / bence@vesselapi.com）

**許諾は4点とも下りた。しかし提供元自身が、この数値を交通量の指標として公開しないよう助言している。**

#### 許諾の内容

| 質問 | 回答 |
|---|---|
| 1. 集計値の公開 | **無料プランのまま許可**（テスト中・公開後とも）。ただし生データは非公開のこと。ダウンロード可能なフィード、当社APIの前段となる公開API、ミラーリング、再販は禁止。「照会した用途はこの範囲に十分収まる」 |
| 2. 出典表示 | **不要**。付けるかどうかは自由、指定の文言もなし。**許諾はアドレスではなく用途に紐づく**ため、独自ドメインへの移行で何も変わらない |
| 3. 広告導入 | 導入時に再連絡すること。その時点で検討する。**自動的にプラン変更を意味するものではない** |
| 4. キーの失効 | 無料キーは90日。**当該キーは 2026-12-02 失効**。残り10分の1（＝9日前）と失効時にメール通知あり。無停止ローテーションは「新キー作成 → ジョブ移行 → 旧キー削除」。**1アカウントで複数キーを保持でき、月間枠はアカウント単位なので2本目を作っても消費は増えない** |

質問2の書き方（許諾を URL でなく用途に紐づけるよう求めた）は意図どおり機能した。

#### データの妥当性についての指摘（こちらが本質）

Bence が同一 bbox を4時間窓で実測した結果:

| 区分 | 隻数 |
|---|---|
| bbox 全体 | 57 |
| **うちケシュム島・バンダルアッバス沖の狭い海域** | **46** |
| **東経 56.1 度以東（実際の通航レーン）** | **6**（うち4隻は錨泊） |
| bbox 全体で 1kt 超で移動中 | 10 |

指摘の要旨:

- この数値は「海峡の交通量」ではなく**当社のカバレッジ範囲**を表すものになる
- 日次の数値は、船舶の動きと同じくらい**カバレッジの変動で動く**
- **読者はホルムズ海峡の交通量の低下を「出来事」として受け取る。この数値をその指標として公表することは勧めない**
- **有料プランでもクレジットパックでも解決しない。**衛星測位は船単位のリクエストであり、エリアを埋めることができない
- 当該海域のカバレッジ改善には取り組んでいるが、**時期は示せない**

#### 当方の実測との照合

`diag_vesselapi.py` の実測（2026-09-03）で停泊16隻としていたものは、
**ホル・ファッカン沖ではなくケシュム島・バンダルアッバス沖（イラン側）の錨泊地**だった。
bbox（lon 55.6-57.0）の捕捉のほとんどが通航レーン（56.1E 以東）の西側に偏っており、
**通航レーンをほとんど見ていなかった**ことになる。

「錨泊待機の急増は封鎖の先行シグナルとして価値がある」という当初の見立ては、
この偏りを踏まえておらず成立しない。

---

## 決定（2026-09-04）

**VesselAPI の実測値は公開しない。「AI 推計のみ・出自を明示して運用」を正式な方針として確定する。**

判断の理由は規約ではなくデータの妥当性。**提供元自身が指標として成立しないと明言しており、
かつ課金で解決しないことも明示されている。**誤った通航量を出す誤報コストは、
推計値を出す誤りより桁違いに重い。

### 維持すること

- **アカウントとキーは維持する**（無料・維持コストゼロ）。
  キーは 2026-12-02 失効。9日前に通知が来るので、その時点でローテーションするか判断する
- **四半期ごとに `diag_vesselapi.py --count` を回し、カバレッジの変化を測る**。
  特に 56.1E 以東の移動中隻数を見る。改善していれば採用を再検討する
- カバレッジが実用水準に達した場合、または広告を導入する場合は、**Bence に再連絡する**
  （本人から「計画が変わったら、またデータをより広く使いたくなったら連絡を」と申し出がある）

### 依頼2への影響

- 実測ソースが当面存在しないため、表示は**推計のみ・出自バッジは常に「AI推計」**になる
- ただし **3-C（三値化）の構造は先に入れておく**。将来カバレッジが改善したときに、
  後から三値化を足すのはデータ構造変更を伴い高くつくため
- `scripts/fetch_ais.py` の VesselAPI への書き換えは**当面不要**（AISstream.io 版のまま無効化を継続）
