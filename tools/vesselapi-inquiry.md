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

（返信が届いたらここに日付・回答内容・それを受けた決定を追記する）
