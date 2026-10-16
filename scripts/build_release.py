"""Package application code only. Personal databases and secrets stay separate."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "app.py", "growth.py", "time_utils.py", "runtime_config.py", "backup_utils.py",
    "manage.py", "wsgi.py", "requirements.txt", "deploy/pythonanywhere_wsgi.py",
    "docs/DEPLOYMENT_PYTHONANYWHERE.md", "docs/GROWTH_V1.md",
    "scripts/build_release.py",
)


def main():
    paths = [ROOT / name for name in FILES]
    paths.extend(sorted((ROOT / "templates").rglob("*.html")))
    for path in paths:
        if not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"公開用ファイルが見つからないか、保存場所が不正です：{path.name}")
    destination = ROOT / "dist" / "liftlog-release.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT).as_posix())
    print(f"公開用ファイルを保存しました：{destination}（{len(paths)}ファイル、{destination.stat().st_size:,}バイト）")
    print("記録データ・秘密鍵・バックアップは含めていません。")


if __name__ == "__main__":
    main()
