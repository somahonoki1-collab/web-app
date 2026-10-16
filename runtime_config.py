"""Shared configuration for local development, WSGI, and management commands."""
import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = Path.home() / ".config" / "liftlog" / "settings.json"
SETTINGS_KEYS = {"WEB_APP_ENV", "WEB_APP_INSTANCE_PATH", "WEB_APP_DATABASE_URI",
                 "WEB_APP_HOST", "FLASK_SECRET_KEY"}


def database_path():
    instance = Path(os.environ.get("WEB_APP_INSTANCE_PATH", str(PROJECT_ROOT / "instance"))).resolve()
    uri = os.environ.get("WEB_APP_DATABASE_URI", "sqlite:///training.db")
    if not uri.startswith("sqlite:///") or "?" in uri:
        raise RuntimeError("管理コマンドにはファイル形式のSQLiteデータベースを指定してください。")
    value = Path(uri[len("sqlite:///"):])
    return (value if value.is_absolute() else instance / value).resolve()


def validate_host(host):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host or "." not in host:
        raise ValueError("ホスト名だけを入力してください（https:// やパスは不要です）。")
    return host


def configure_environment(production=False):
    if production:
        os.environ["WEB_APP_ENV"] = "production"
    mode = os.environ.get("WEB_APP_ENV", "development")
    if mode not in ("development", "production"):
        raise RuntimeError("WEB_APP_ENV は development または production を指定してください。")
    if mode == "production" or os.environ.get("WEB_APP_CONFIG_FILE"):
        location = Path(os.environ.get("WEB_APP_CONFIG_FILE", str(DEFAULT_CONFIG_FILE))).expanduser()
        if location.is_file():
            try:
                settings = json.loads(location.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RuntimeError("公開用設定ファイルを読み込めません。JSON形式を確認してください。") from None
            if not isinstance(settings, dict):
                raise RuntimeError("公開用設定ファイルはJSONオブジェクトにしてください。")
            for key in SETTINGS_KEYS:
                value = settings.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise RuntimeError("公開用設定の値は文字列にしてください。")
                    os.environ.setdefault(key, value)
    production = os.environ.get("WEB_APP_ENV", "development") == "production"
    if production:
        if len(os.environ.get("FLASK_SECRET_KEY", "")) < 32:
            raise RuntimeError("公開用の秘密鍵が必要です。manage.py init-config で設定を作成してください。")
        try:
            validate_host(os.environ.get("WEB_APP_HOST", ""))
        except ValueError:
            raise RuntimeError("公開先のホスト名をWEB_APP_HOSTに設定してください。") from None
        instance_value = os.environ.get("WEB_APP_INSTANCE_PATH", "")
        if not instance_value or not Path(instance_value).is_absolute():
            raise RuntimeError("公開用データ保存先には絶対パスを指定してください。")
        instance = Path(instance_value).resolve()
        target = database_path()
        if instance.is_relative_to(PROJECT_ROOT) or target.is_relative_to(PROJECT_ROOT):
            raise RuntimeError("公開用データはコードのフォルダーの外に保存してください。")
        if not target.is_file():
            raise RuntimeError("公開用の記録データが見つかりません。バックアップを配置してから起動してください。")
    return production
