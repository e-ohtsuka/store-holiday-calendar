"""Build StoreCalendar v1.12 for Windows (PyInstaller onedir).

Run on Windows. Private schedules and settings are EXCLUDED from the sharing ZIP.
The local dist/StoreCalendar/data is preserved across rebuilds.
"""
from __future__ import annotations
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = 'StoreCalendarDemo'
TARGET = ROOT / 'dist' / APP_NAME
VERSION = 'v1.12'


def run(*args: str) -> None:
    print('\n>', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(args), cwd=ROOT, check=True)


def ensure_dependencies():
    if any(importlib.util.find_spec(m) is None for m in ('fitz', 'PIL')):
        run(sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt')
    if importlib.util.find_spec('PyInstaller') is None:
        run(sys.executable, '-m', 'pip', 'install', 'pyinstaller>=6.22,<7')


def build() -> int:
    if sys.platform != 'win32':
        print('Windows用EXEの生成はWindows PC上で実行してください。')
        return 1
    if not all((ROOT / 'assets' / n).is_file() for n in
               ('background_vector.json', 'store_logo.pdf', 'holidays_builtin_2026_2027.csv')):
        print('assetsフォルダーが不完全です。ZIPをすべて展開してください。')
        return 1
    ensure_dependencies()
    with tempfile.TemporaryDirectory(prefix='store_calendar_build_') as tmp:
        tmp = Path(tmp)
        old_data = TARGET / 'data'
        backup = tmp / 'saved_data'
        # PyInstaller --noconfirmでdistが削除されるため、既存ユーザーデータを退避。
        source_data = ROOT / 'data'
        src = (old_data if old_data.is_dir() and any(old_data.iterdir())
               else source_data if source_data.is_dir() else old_data)
        if src.is_dir():
            shutil.copytree(src, backup)
            print(f'保存データをバックアップ: {src}')
        command = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                   '--onedir', '--windowed', '--name', APP_NAME,
                   '--add-data', 'assets:assets', '--collect-all', 'pymupdf',
                   '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'),
                   '--specpath', str(ROOT), str(ROOT / 'store_calendar.py')]
        try:
            run(*command)
        finally:
            if backup.exists():
                old_data.mkdir(parents=True, exist_ok=True)
                shutil.copytree(backup, old_data, dirs_exist_ok=True)
        exe = TARGET / (APP_NAME + '.exe')
        if not exe.is_file():
            raise FileNotFoundError(f'EXEが見つかりません: {exe}')
        # 操作説明はオフラインで読めるHTML（外部通信不要）。
        if (ROOT / '使い方.html').is_file():
            shutil.copy2(ROOT / '使い方.html', TARGET / '使い方.html')
        if (ROOT / 'guide_images').is_dir():
            shutil.copytree(ROOT / 'guide_images', TARGET / 'guide_images', dirs_exist_ok=True)
        if (ROOT / 'はじめに.txt').is_file():
            shutil.copy2(ROOT / 'はじめに.txt', TARGET / 'はじめに.txt')
        # 公開・配布ZIPから個人情報入りのdataフォルダーを確実に除外する。
        # EXE本体は初回起動時に新しいdataフォルダーを作成する。
        stage = tmp / 'public_stage' / APP_NAME
        shutil.copytree(TARGET, stage, ignore=shutil.ignore_patterns('data',
                      'last_error.txt', '*.log', '__pycache__'))
        output = shutil.make_archive(str(ROOT / 'dist' /
                          f'{APP_NAME}_windows_{VERSION}'), 'zip',
                          root_dir=tmp / 'public_stage', base_dir=APP_NAME)
    print('\n==== EXE作成完了 ====')
    print('本体:', exe)
    print('配布ZIP:', output)
    print('配布ZIPには店舗の休業日や設定データは含めていません。')
    print('別PCではZIPを「すべて展開」してEXEを起動してください。')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(build())
    except (OSError, subprocess.CalledProcessError) as exc:
        print('\nEXE作成失敗:', exc)
        raise SystemExit(1)
