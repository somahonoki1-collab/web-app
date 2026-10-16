"""Offline administration. Passwords are entered through a hidden prompt."""
import argparse
import getpass
import json
import os
from pathlib import Path
from secrets import token_hex

from backup_utils import backup_database
from runtime_config import DEFAULT_CONFIG_FILE, PROJECT_ROOT, configure_environment, database_path, validate_host


def main():
    parser = argparse.ArgumentParser(description="LIFT LOGの公開設定・バックアップ・本人確認後のパスワード再設定")
    parser.add_argument("--production", action="store_true", help="公開用設定を使う")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("init-config", help="公開用の秘密鍵・データ保存先を作成")
    setup.add_argument("--host", required=True, help="例：yourname.pythonanywhere.com")
    setup.add_argument("--data-dir", default=str(Path.home() / "liftlog-data"))
    backup = commands.add_parser("backup", help="アプリを読み込まずSQLiteをバックアップ")
    backup.add_argument("--output", help="未使用の保存先ファイル名")
    reset = commands.add_parser("reset-password", help="ホスティング管理者が本人確認後に実行")
    reset.add_argument("username")
    reset.add_argument("--verified", action="store_true", required=True, help="本人確認が済んだことを明示")
    args = parser.parse_args()
    try:
        if args.command == "init-config":
            host = validate_host(args.host.strip().lower())
            if not host.endswith(".pythonanywhere.com"):
                raise ValueError("無料公開用の pythonanywhere.com のホスト名を指定してください。")
            directory = Path(args.data_dir).expanduser().resolve()
            if directory.is_relative_to(PROJECT_ROOT):
                raise ValueError("データ保存先はコードのフォルダーの外を指定してください。")
            directory.mkdir(parents=True, exist_ok=True)
            config = Path(os.environ.get("WEB_APP_CONFIG_FILE", str(DEFAULT_CONFIG_FILE))).expanduser()
            if config.resolve().is_relative_to(PROJECT_ROOT):
                raise ValueError("秘密鍵の設定ファイルはコードのフォルダーの外に保存してください。")
            config.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"WEB_APP_ENV": "production", "WEB_APP_HOST": host,
                    "WEB_APP_INSTANCE_PATH": str(directory),
                    "WEB_APP_DATABASE_URI": "sqlite:///" + (directory / "training.db").as_posix(),
                    "FLASK_SECRET_KEY": token_hex(32)}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            print(f"公開用設定を作成しました：{config}")
            print(f"既存記録のバックアップを配置する先：{directory / 'training.db'}")
            return
        configure_environment(production=args.production)
        source = database_path()
        if args.command == "backup":
            print(f"バックアップを保存しました：{backup_database(source, args.output)}")
            return
        first = getpass.getpass("新しいパスワード（8〜128文字）：")
        second = getpass.getpass("確認用パスワード：")
        if not 8 <= len(first) <= 128 or first != second:
            raise ValueError("パスワードの長さ・一致を確認してください。変更は行っていません。")
        snapshot = backup_database(source)
        from app import app, db, User
        from werkzeug.security import generate_password_hash
        with app.app_context():
            user = User.query.filter_by(username=args.username.strip().lower()).first()
            if user is None:
                raise ValueError("指定されたユーザーが見つかりません。")
            user.password_hash = generate_password_hash(first)
            user.auth_version += 1
            db.session.commit()
        print(f"パスワードを変更し、既存のログインを無効化しました。変更前のバックアップ：{snapshot}")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"処理を完了できませんでした：{error}\n")


if __name__ == "__main__":
    main()
