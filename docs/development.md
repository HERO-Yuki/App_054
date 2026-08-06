# 開発者向け: テストとローカル実行の手順

利用者向けの手順は [README.md](../README.md) を参照。これは開発・検証用のメモ。

## テストの実行

初回のみ(仮想環境の作成と依存のインストール):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

テスト実行(ネットワーク接続不要。Steam/Discord には一切アクセスしない):

```powershell
.venv\Scripts\python -m pytest tests/ -v
```

## ローカルでのドライラン(実データで検証)

実際に Steam API を叩き、Discord には**送信せず**にペイロードを標準出力へ JSON 表示する。
`state.json` も更新されない。

```powershell
$env:STEAM_API_KEY = "自分のAPIキー"
$env:STEAM_ID = "自分のSteamID64"
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/…"
$env:PYTHONUTF8 = "1"
.venv\Scripts\python -m src.main run --dry-run
```

※ PowerShell の履歴にキーが残るのが気になる場合は、実行後に `Clear-History` するか、
一時的な環境変数設定用のスクリプト(コミットしない)を使うこと。

## サブコマンド

| コマンド | 用途 |
|---|---|
| `python -m src.main run [--dry-run]` | 本体(取得→差分→通知→state保存) |
| `python -m src.main update-state --status failed` | last_run のみ更新(workflow の失敗時ステップが使用) |
| `python -m src.main notify-error --run-url <URL> [--dry-run]` | エラー通知の送信(workflow の失敗時ステップが使用) |

## わざと失敗させてエラー通知を確認する方法

リポジトリの Secrets の `STEAM_ID` を一時的に `0` などに変えて Run workflow
→ Discord にエラー通知が届き、`state.json` の `last_run_status` が `failed` でコミットされることを確認
→ 確認後、正しい値に戻す。
