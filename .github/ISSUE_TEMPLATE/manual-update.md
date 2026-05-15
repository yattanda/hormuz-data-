---
name: 手動データ更新
about: manual-update.json の定期更新チェックリスト
title: "手動更新 YYYY-MM-DD"
labels: manual-update
assignees: yattanda
---

## 📋 手動データ更新チェックリスト

### 🎯 シナリオ確率
- [ ] `A_diplomacy_pct` 外交解決シナリオ確率（%）
- [ ] `B_partial_blockade_pct` 部分封鎖シナリオ確率（%）
- [ ] `C_full_blockade_pct` 完全封鎖シナリオ確率（%）
- [ ] `D_escalation_pct` エスカレーションシナリオ確率（%）
> 合計が **100%** になることを確認

---

### 🛢 石油・市場データ
- [ ] `hormuz_daily_flow_mbpd` ホルムズ通過量（Kpler/Argus参照）
- [ ] `hormuz_normal_flow_mbpd` 通常時通過量（変更がなければそのまま）
- [ ] `flow_disruption_pct` 流量disruption率（自動計算: 1 - daily/normal × 100）
- [ ] `war_risk_premium_manual` 確認できる場合のみ数値をセット（例: 2.0）。確認できない場合は `null` のまま
- [ ] `war_risk_premium_verified` 確認済みなら `true`、未確認なら `false`
- [ ] `war_risk_premium_source` 確認済みの場合のみ出典と日付を記載（例: "S&P Global 2026/05/15"）

---

### 📅 重要日程・テキスト
- [ ] `critical_date` 次の重要日程（YYYY-MM-DD形式）
- [ ] `critical_note` 重要日程の説明
- [ ] `last_manual_note` 最新状況メモ（簡潔に）

---

### 🔗 参照ソース
- [ ] [Argus Media](https://www.argusmedia.com/) – 保険料率・流量
- [ ] [EIA Weekly](https://www.eia.gov/petroleum/supply/weekly/) – 石油統計（自動更新確認）
- [ ] [UANI](https://www.unitedagainstnucleariran.com/) – 制裁・封鎖情報

---

### ✅ 完了作業
- [ ] `data/manual-update.json` の数値を更新
- [ ] `updated_at` を現在時刻に更新（JST）
- [ ] VS Code で `Ctrl+S` 保存
- [ ] Commit メッセージ: `chore: manual update YYYY-MM-DD`
- [ ] Push 完了
- [ ] `https://yattanda.github.io/hormuz-data-/` で表示確認
