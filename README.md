# thread-bot

Discordのスレッドを管理するBOTです。一覧の表示、ボタンからの作成、自動クローズができます。

## 動作環境（参考）

- Python 3.12.14
- discord.py 2.7.1
- aiosqlite 0.22.1
- python-dotenv 1.2.4

## 初期設定

- `thread.py`と同じ場所の`.env`に`TOKEN=BOTのトークン`を設定。
- [Discord Developer Portal](https://discord.com/developers/applications)でServer Members IntentとMessage Content IntentをONにする。
- `bot`と`applications.commands`のスコープでBotをサーバーに招待。
- `thread.py`を起動し、サーバー管理者が管理するチャンネルで`/setup`を実行。

## コマンド

- `/setup` - スレッドパネルを設置。
- `/update` - パネルを強制更新。
- `/config` - 自動クローズ日数、作成停止、表示件数、ログ送信先を設定。
- `/show_config` - 現在の設定を表示。
- `/admin add` - BOT管理者を登録。
- `/admin remove` - BOT管理者を解除。
- `/admin list` - BOT管理者の一覧を表示。
- `/rename` - スレッド名を変更（30文字まで）。
- `/close` - スレッドを閉じる。

`/rename`と`/close`はスレッド内で作成者か管理者が実行できます。管理者の登録・解除はサーバー管理者かBOT所有者のみ、その他は管理者用です。

## 補足

- 管理対象のチャンネルに直接投稿したメッセージは削除されます。会話はスレッド内で行ってください。
- スレッドを閉じてもメッセージは残ります。
- `/config`の`delete_panel`では開いているスレッドもまとめて閉じます。
- 設定は`threads.db`に保存されます。
