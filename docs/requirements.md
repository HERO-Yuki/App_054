---
title: Steam所有ゲーム セール通知ツール 要件定義書
status: confirmed
version: 1.0
date: 2026-08-05
tags:
  - project
  - steam
  - discord
  - github-actions
  - requirements
---

# Steam所有ゲーム セール通知ツール 要件定義書 v1.0

## 1. 背景・目的

### 解決する課題
Steam のウィッシュリストには「セール開始通知」があるが、**購入済みゲームには通知が来ない**。
「自分が遊んだことのあるゲームを紹介する」ことを趣味とするユーザーにとって、
所有ゲームのセールは紹介の絶好のタイミングだが、それを知る手段が存在しない。

### 目的
| 段階 | 目的 |
|---|---|
| 一次 | 友人への贈答（確実に動き、放置しても止まらないこと） |
| 二次 | 一般配布（README のみで自力セットアップが完結すること） |

### 想定ユーザー像
- 所有ゲーム数：数十〜数百本
- Discord を日常的に使用
- Git / GitHub の基本操作は経験あり
- プログラミングによる改修は前提としない

---

## 2. スコープ

### v1.0 に含むもの
- Steam の所有ゲームのうち、現在セール中のものを抽出
- **1日1回**の自動実行（GitHub Actions cron）
- Discord Webhook への通知
- 重複通知の抑制（差分管理）
- 手動実行トリガー
- 実行失敗時の Discord へのエラー通知
- README によるセットアップ手順の提供

### v1.0 に含まないもの
- Steam 以外のストア対応
- 歴代最安値・底値判定
- 紹介文（SNS投稿用テキスト）の自動生成
- プレイ時間による絞り込み
- GUI / Web UI
- 手順書スライド（README に統合）
- 失敗時のメール通知（GitHub 標準機能は OFF にする方針）

---

## 3. 機能要件

### FR-01 所有ゲーム一覧の取得

- エンドポイント：`https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/`
- パラメータ：`key`, `steamid`, `include_appinfo=1`, `include_played_free_games=1`, `skip_unvetted_apps=false`, `format=json`
- 取得項目：`appid`, `name`, `playtime_forever`
- 頻度：実行のたびに取得（1日1回なので負荷は無視できる）

**制約（重要）**
- 対象アカウントのプライバシー設定「ゲームの詳細」が公開でないと空レスポンスが返る
- ただし **API キーが対象 SteamID 本人のものであれば、プロフィール非公開のままでも取得可能**
- 本ツールは「利用者が自分のキーで自分のライブラリを見る」構成のため、**プロフィール公開は不要**
- 空レスポンス時は「プライバシー設定またはキーの不一致」を示す明確なエラーメッセージを出す

### FR-02 価格・割引情報の取得

- エンドポイント：`https://store.steampowered.com/api/appdetails`
- パラメータ：`appids=<id>`, `cc=jp`, `l=japanese`, `filters=price_overview`
- 取得項目：`initial`, `final`, `discount_percent`, `final_formatted`, `initial_formatted`

**実装上の注意**
- `filters=price_overview` 指定時にカンマ区切りの複数 appid が有効かを**実装初期に検証すること**
  - 有効なら 50〜100件ずつのバッチ取得とし、実行時間を大幅短縮
  - 無効なら1件ずつ取得し、リクエスト間に 1.0〜1.5 秒の待機を挟む
- 非公式 API のためレート制限は非公開。**429 を受けたら指数バックオフ**で待機
- レスポンス `success: false` のもの（販売終了・地域未対応）はスキップ
- `price_overview` が存在しないもの（無料ゲーム）はスキップ

### FR-03 セール判定

- `discount_percent > 0` をセール中と判定
- `config.yaml` の `min_discount`（既定値 20）未満は通知対象外
- 通貨・地域は `config.yaml` の `country_code`（既定値 `jp`）で指定

### FR-04 差分管理

**通知条件**

| 状態変化 | 通知 |
|---|---|
| セール対象外 → セール中 | する（新規セール） |
| 割引率が前回より上昇 | する（さらに値下げ） |
| セール中で割引率が同じ | しない |
| セール中 → 対象外 | しない |

**状態ファイル `state.json`**

```json
{
  "schema_version": 1,
  "last_run": "2026-08-05T10:17:32+09:00",
  "last_run_status": "success",
  "games": {
    "620": {
      "name": "Portal 2",
      "discount_percent": 90,
      "final_price": 205,
      "notified_at": "2026-08-05T10:17:32+09:00"
    }
  }
}
```

- **`last_run` は実行結果にかかわらず毎回必ず更新し、毎日コミットする**
  （これが FR-08 のキープアライブを兼ねる）
- API 取得に失敗したゲームの既存エントリは削除せず維持する（取りこぼし回復のため）

### FR-05 初回実行時の挙動

state.json が存在しない初回実行では、セール中の全所有ゲームが「新規」と判定され大量通知が発生する。これを回避する。

- 初回は**サマリー形式**で通知する
  - 「セットアップ完了。現在 N 本の所有ゲームがセール中です」
  - 割引率上位10件のみを列挙
  - 「明日以降は新しくセールが始まったものだけをお知らせします」と明記
- `config.yaml` の `first_run_summary`（既定値 `true`）で制御

### FR-06 Discord 通知

- Discord Webhook へ POST（Bot ではなく Webhook を使用。常時稼働・権限管理が不要なため）
- 通知内容：ゲーム名 / 割引率 / 現在価格 / 定価 / ストアURL / ヘッダー画像

**件数制限への対応**
- Discord の制約：1メッセージ 2000文字、embed は最大10個/メッセージ
- 割引率の**降順**にソート
- 1メッセージあたり embed 10件まで、複数メッセージに分割して送信
- `config.yaml` の `max_notify`（既定値 30）で上限を設定
- 上限を超えた場合は末尾に「他 N 件」と付記

### FR-07 定期実行と手動実行

```yaml
on:
  schedule:
    - cron: '17 10 * * *'
      timezone: 'Asia/Tokyo'
  workflow_dispatch:
```

- 実行時刻：**JST 10:00 台**
  - Steam のセール切替は太平洋時間 10:00 頃＝JST 深夜2〜3時に発生することが多く、朝の実行で確実に拾える
- 分は `17` など半端な値にする（毎時00分はランナー混雑で遅延しやすい）
- `timezone:` フィールドは2026年3月以降に追加された機能。**動作しない場合は UTC 表記 `'17 1 * * *'` にフォールバックする**
- `workflow_dispatch` は必須。セットアップ直後の動作確認に使用する

### FR-08 キープアライブ（自動無効化対策）

GitHub はリポジトリに**60日間コミットがないとスケジュールワークフローを自動的に無効化**する。しかもエラーもバナーも出ないまま静かに停止するため、対策が必須。

**一次対策（主）**
- FR-04 の通り、**セールに変化がない日も `last_run` を更新して毎日コミットする**
- これにより毎日コミットが発生し、60日カウンタが常にリセットされる

**二次対策（保険）**
- 毎月1日の実行時に、GitHub API でワークフローを明示的に有効化する

```yaml
permissions:
  contents: write
  actions: write
```

```bash
gh api -X PUT repos/${{ github.repository }}/actions/workflows/daily.yml/enable
```

- 失敗しても本体処理には影響させない（`continue-on-error: true`）

### FR-09 エラー処理と通知

- API 呼び出し失敗時：指数バックオフで最大3回リトライ
- 致命的失敗時：**Discord にエラーを投稿**（`if: failure()` ステップ）
  - 投稿内容：発生日時、エラー種別、Actions の実行ログへのリンク
  - 通常通知とは別 Webhook を指定できるようにする（`DISCORD_ERROR_WEBHOOK_URL`。未設定なら通常 Webhook へ）
- **GitHub 標準の失敗メール通知は使用しない**
  - README に「Settings → Notifications → Actions のメール通知を OFF にする」手順を記載
- 失敗時も `last_run` と `last_run_status: "failed"` は更新してコミットする（キープアライブ維持のため）

### FR-10 セキュリティ要件

- 認証情報をソースコード・ログ・コミットに一切含めない
- **Webhook URL や API キーを標準出力に出さない**（デバッグ出力でも禁止）
- ログに出す際は必ずマスクする
- `.gitignore` に `.env` を含める

---

## 4. 非機能要件

| 項目 | 要件 |
|---|---|
| 実行時間 | 所有300本で1回の実行が10分以内 |
| 冪等性 | 同じ状態で2回実行しても通知が重複しない |
| 依存 | 外部有料サービスを使わない |
| ライブラリ | 標準ライブラリ＋`requests`＋`PyYAML` のみ（配布容易性のため最小化） |
| ログ | 実行日時・所有本数・セール件数・通知件数・エラーを記録 |
| 言語 | Python 3.12 |

---

## 5. 配布構成

### リポジトリ方針

| 種別 | 可視性 | 用途 |
|---|---|---|
| テンプレートリポジトリ | **Public** | 配布用。コードと README のみ。state.json は含まない |
| 各利用者の稼働リポジトリ | **Private 推奨** | 「Use this template」で作成。state.json に所有ゲーム一覧が入るため |

**Fork ではなく Template を使う理由**
- Public リポジトリを Fork したものは Private に変更できない
- Template なら作成時に可視性を選択できる
- Public リポジトリの Fork ではスケジュールワークフローが既定で無効化される

**GitHub Actions の費用**
- Public：標準ランナーは分数無制限・無料
- Private：Free プランで月2,000分。本ツールは1日1回・数分のため月240分程度で収まる

### 利用者が登録する Secrets

| 名前 | 内容 | 必須 |
|---|---|---|
| `STEAM_API_KEY` | Steam Web API キー | ○ |
| `STEAM_ID` | SteamID64（17桁の数字） | ○ |
| `DISCORD_WEBHOOK_URL` | 通知先チャンネルの Webhook URL | ○ |
| `DISCORD_ERROR_WEBHOOK_URL` | エラー通知先（未設定なら上と同じ） | － |

### セットアップ手順（README に記載する流れ）

1. Steam API キーを取得（`steamcommunity.com/dev/apikey`）
   - Steam Guard の有効化が必要
   - 「ドメイン名」欄は任意の値でよい（つまずきポイントとして明記）
2. SteamID64 を確認（カスタムURL設定時は数字が見えないため確認方法を図示）
3. Discord で Webhook を作成
   - **サーバー設定ではなくチャンネル設定 → 連携サービス → ウェブフック**
4. 「Use this template」で自分のリポジトリを作成（Private 推奨）
5. Settings → **Secrets and variables → Actions** に上記を登録
   - Codespaces / Dependabot タブと隣接しており誤りやすい旨を明記
6. Actions タブでワークフローを有効化
7. **「Run workflow」で手動実行し、Discord に届くことを確認**
8. GitHub の失敗メール通知を OFF にする

---

## 6. ファイル構成

```
steam-sale-notifier/
├── .github/
│   └── workflows/
│       └── daily.yml
├── src/
│   ├── main.py
│   ├── steam.py          # 所有ゲーム・価格取得
│   ├── state.py          # state.json の読み書きと差分判定
│   ├── notifier.py       # Discord 通知
│   └── config.py         # 設定読み込み
├── tests/
├── config.yaml
├── state.json            # 実行時に自動生成・自動コミット
├── requirements.txt
├── README.md
├── LICENSE               # MIT
└── .gitignore
```

### config.yaml（既定値）

```yaml
country_code: jp
language: japanese
min_discount: 20
max_notify: 30
first_run_summary: true
request_interval_sec: 1.2
```

---

## 7. ライセンス

**MIT License** を採用する。

- 許可：商用利用・改変・再配布・私的利用のすべて
- 条件：LICENSE ファイル（著作権表示＋許諾文）の同梱のみ
- 免責：無保証。損害について作者は責任を負わない

**README に併記する免責事項**
- 本ツールは Valve Corporation とは無関係の非公式ツールである
- Steam Store API は非公式エンドポイントであり、予告なく仕様変更・停止する可能性がある
- 過度なリクエストを行わない設計であることを明記する

---

## 8. 未決事項

| # | 論点 | 判断時期 |
|---|---|---|
| 1 | `filters=price_overview` の複数 appid 指定が有効か | 実装初期に検証 |
| 2 | `timezone:` フィールドが実際に動作するか | 初回デプロイ時に検証 |
| 3 | DLC の扱い | v1.1 以降 |
| 4 | プレイ時間0のゲームを除外するか | v1.1 |

---

## 9. 成功の定義

- 友人が**通知をミュートせずに使い続けている**
  → 差分管理・件数制限・初回サマリーが機能している証拠
- セットアップで詰まらず、README だけで完了できた
- 60日経過後も通知が止まっていない
