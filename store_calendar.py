"""休業日カレンダー作成ツール v1.12 / Store Holiday Calendar.

画面では完成デザインの1枚を編集し、A4横3列×2段の6枚をPDFに出力する。
GUIはTkinter、PDF描画はPyMuPDF、画面表示用の画像処理はPillowを使用する。

主な設計方針:
- 日付クリック時はそのセルの画像だけを差し替え、PDFの再生成を避ける。
- 月別休業日はJSONに自動保存し、別月に移動しても復元する。
- 配布素材は同梱するが、編集データは実行場所側の書き込み可能な領域へ保存する。
- 祝日は内閣府CSVをローカルキャッシュし、オフラインでも使用できるようにする。
- フォントを再配布せず、利用PCのインストール済みフォントを使用する。

使い方: python store_calendar.py
簡易PDFテスト: python store_calendar.py --demo --year 2026 --month 11 --output test.pdf
"""
from __future__ import annotations

import argparse
import calendar
import colorsys
import hashlib
import shutil
import csv
import datetime as dt
import io
import json
import math
import os
import re
import tempfile
import traceback
import sys
import threading
import urllib.request
import unicodedata
from pathlib import Path
from typing import Any

import fitz

# The assets bundled by PyInstaller are in _internal (sys._MEIPASS).
# User edits must NOT be written there: they belong next to the EXE if writable.
APP_ID = 'StoreCalendarDemo'  # ユーザー領域への保存時に用いるフォルダ名
FROZEN = bool(getattr(sys, 'frozen', False))
RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BASE = APP_DIR  # Keep the source-tree layout for Python/VS Code users.


def _writable_dir(path: Path) -> bool:
    """Verify actual write permission (is_dir alone is not sufficient)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False


def user_config_root() -> Path:
    """Per-user application preferences, kept separate from holiday/edit data.

    Save locations are installation-specific: portfolio/demo copies must not
    unexpectedly share a production calendar's data directory.
    """
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / APP_ID / 'installations'


# The preference file only contains the chosen directory path, not business data.
_INSTALL_KEY = hashlib.sha256(os.path.normcase(str(APP_DIR)).encode('utf-8')).hexdigest()[:16]
DATA_LOCATION_FILE = user_config_root() / f'{_INSTALL_KEY}.json'
DATA_DIR_WARNING = ''


def _configured_data_dir() -> Path | None:
    try:
        raw = json.loads(DATA_LOCATION_FILE.read_text(encoding='utf-8'))
        value = raw.get('data_dir')
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser().resolve()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def choose_data_dir() -> tuple[Path, bool]:
    """Portable data next to EXE; fallback to per-user profile if read-only.

    The fallback is never silent: the GUI displays the actual location.
    Previous source-mode data remains next to the .py file.
    """
    global DATA_DIR_WARNING
    saved = _configured_data_dir()
    if saved is not None:
        if _writable_dir(saved):
            return saved, False
        DATA_DIR_WARNING = f'設定済みの保存先を利用できません: {saved}（標準の保存先で起動中）'
    portable = APP_DIR / 'data'
    if _writable_dir(portable):
        return portable, False
    if sys.platform == 'win32':
        user_base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    else:
        user_base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share')))
    fallback = user_base / APP_ID / 'data'
    if not _writable_dir(fallback):
        raise OSError(f'編集データの保存先に書き込めません: {portable} / {fallback}')
    return fallback, True


DATA, DATA_IS_FALLBACK = choose_data_dir()
SETTINGS_FILE = DATA / 'settings.json'
CLOSURES_FILE = DATA / 'closed_days.json'
HOLIDAYS_FILE = DATA / 'syukujitsu.csv'
HOLIDAY_META_FILE = DATA / 'holiday_update.json'


def _activate_data_dir(directory: Path) -> None:
    """Change all runtime paths together (GUI, offline holidays and CLI)."""
    global DATA, DATA_IS_FALLBACK, DATA_DIR_WARNING
    global SETTINGS_FILE, CLOSURES_FILE, HOLIDAYS_FILE, HOLIDAY_META_FILE
    DATA = directory
    DATA_IS_FALLBACK = False
    DATA_DIR_WARNING = ''
    SETTINGS_FILE = DATA / 'settings.json'
    CLOSURES_FILE = DATA / 'closed_days.json'
    HOLIDAYS_FILE = DATA / 'syukujitsu.csv'
    HOLIDAY_META_FILE = DATA / 'holiday_update.json'


def switch_data_directory(destination: Path, settings: dict | None = None,
                          use_existing: bool = False) -> Path:
    """Switch storage safely without deleting or overwriting old calendars.

    - Copy mode: copy the current files to a selected directory. Reject name
      conflicts instead of accidentally overwriting someone's previous data.
    - Existing mode: intentionally use an existing calendar dataset without
      copying anything into it. Only call after explicit UI confirmation.
    - The preference is committed only AFTER the destination is usable.
    """
    destination = Path(destination).expanduser().resolve()
    if destination == DATA.resolve():
        return DATA
    if not _writable_dir(destination):
        raise OSError(f'保存先フォルダーに書き込めません: {destination}')
    source_files = [p for p in DATA.iterdir() if p.is_file()
                    and not p.name.endswith('.tmp')]
    if use_existing:
        if not (destination / 'closed_days.json').is_file() or not (destination / 'settings.json').is_file():
            raise ValueError('既存データを使用するにはsettings.jsonとclosed_days.jsonの両方が必要です。')
    else:
        duplicates = [p.name for p in source_files if (destination / p.name).exists()]
        if (destination / 'settings.json').exists():
            duplicates.append('settings.json')
        if duplicates:
            raise FileExistsError('選択先に同名のデータがあります。上書きせず保護しました: '
                                  + ', '.join(sorted(set(duplicates))))
    created: list[Path] = []
    try:
        if not use_existing:
            for source in source_files:
                target = destination / source.name
                shutil.copy2(source, target)
                created.append(target)
            if settings is not None:
                target = destination / 'settings.json'
                save_json(target, settings)
                if target not in created:
                    created.append(target)
        # Preference file lives under the user's local profile, not in data/.
        save_json(DATA_LOCATION_FILE, {'data_dir': str(destination)})
    except (OSError, ValueError):
        for target in created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    _activate_data_dir(destination)
    return destination
ASSETS = RESOURCE_DIR / 'assets'
KUMO_GEOMETRY = ASSETS / 'background_vector.json'
NAME_LOGO_PDF = ASSETS / 'store_logo.pdf'
BUILTIN_HOLIDAYS = ASSETS / 'holidays_builtin_2026_2027.csv'
OFFICIAL_CSV = 'https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv'

PAGE_W, PAGE_H = fitz.paper_size('a4-l')
CELL_W, CELL_H = PAGE_W / 3, PAGE_H / 2
GRID_W, GRID_H = 232.5, 194.4
GRID_X, GRID_Y = 51.2, 48.0
TOP_BAR_Y, BOTTOM_BAR_Y, BOTTOM_BAR_END = 18.1, 266.3, 287.5
HEADER_H = 25.0
NAVY = (50/255, 60/255, 123/255)
LINE = (.191, .36, .5776)
PINK = (0.9667, 0.6946, 0.8087)
MAGENTA = (.97, .03, .46)
SATURDAY = (0, .47, .75)
BLACK = (.13, .13, .14)
WHITE = (1., 1., 1.)
DEFAULT_SETTINGS = {
    'notice': 'ご予約は前日までにお願い申し上げます。',
    'phone': 'TEL:000-0000-0000',
    'logo_color': '#639AC6',
    'logo_brightness': 100,
    'logo_saturation': 100,
    'logo_opacity': 22,
    'closure_opacity': 38,
    'cut_guides': True,
    'calendar_font_file': '',  # Optional installed TTF/OTF, never bundled
    'position_mm': {'columns': [0.0, 0.0, 0.0], 'rows': [0.0, 0.0]},
}
BACKGROUND_KEYS = ('logo_color', 'logo_brightness', 'logo_saturation', 'logo_opacity')
DEFAULT_RESTS = {'2026-11': [4, 11, 18, 25]}
FALLBACK_HOLIDAYS = {'2026-11-03': '文化の日', '2026-11-23': '勤労感謝の日'}


def save_json(path: Path, obj: Any) -> None:
    """JSONを一時ファイルに書いてから置換し、中途半端な保存を減らす。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def color_for_logo(settings: dict) -> tuple[float, float, float]:
    hx = str(settings.get('logo_color', '#639AC6')).lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', hx):
        hx = '639AC6'
    rgb = [int(hx[i:i+2], 16)/255 for i in (0, 2, 4)]
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    s = max(0, min(1, s*float(settings.get('logo_saturation', 100))/100))
    v = max(0, min(1, v*float(settings.get('logo_brightness', 100))/100))
    return colorsys.hsv_to_rgb(h, s, v)


def opacity(settings: dict, key: str, default: int) -> float:
    return max(0., min(1., float(settings.get(key, default))/100))


def parse_holiday_csv(contents: bytes, min_records: int = 100) -> dict[str, str]:
    """内閣府CSV（主にShift-JIS）を「ISO日付:祝日名」の辞書へ変換する。"""
    text = None
    for encoding in ('cp932', 'utf-8-sig', 'utf-8'):
        try:
            text = contents.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError('祝日CSVを読み取れませんでした。')
    result: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            date = dt.datetime.strptime(row[0].strip(), '%Y/%m/%d').date()
        except ValueError:
            continue
        label = row[1].strip()
        if label:
            result[date.isoformat()] = label
    if len(result) < min_records:
        raise ValueError('取得した祝日CSVに十分な日付がありません。')
    return result


def read_holidays() -> dict[str, str]:
    """ネット接続なしでも内蔵CSVまたは保存済みCSVから祝日を読み込む。"""
    try:
        fallback = parse_holiday_csv(BUILTIN_HOLIDAYS.read_bytes(), min_records=30)
    except (OSError, ValueError):
        fallback = dict(FALLBACK_HOLIDAYS)
    if not HOLIDAYS_FILE.exists():
        return fallback
    try:
        return parse_holiday_csv(HOLIDAYS_FILE.read_bytes())
    except (OSError, ValueError):
        return fallback


def holiday_update_due(now: dt.datetime | None = None) -> bool:
    if not HOLIDAYS_FILE.exists():
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    data = load_json(HOLIDAY_META_FILE, {})
    try:
        previous = dt.datetime.fromisoformat(data['last_success'])
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=dt.timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    return now - previous >= dt.timedelta(hours=24)


def download_holidays() -> dict[str, str]:
    """取得内容を検査してからキャッシュを置換し、破損データを残さない。"""
    request = urllib.request.Request(OFFICIAL_CSV, headers={'User-Agent': 'StoreCalendar/1.11'})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read(1024 * 1024)
    records = parse_holiday_csv(payload)  # Validate before replacing a working cache.
    tmp = HOLIDAYS_FILE.with_suffix('.tmp')
    tmp.write_bytes(payload)
    tmp.replace(HOLIDAYS_FILE)
    save_json(HOLIDAY_META_FILE, {
        'last_success': dt.datetime.now(dt.timezone.utc).isoformat(),
        'count': len(records), 'source': OFFICIAL_CSV,
    })
    return records


def find_jp_font() -> tuple[str | None, fitz.Font]:
    candidates = []
    if sys.platform.startswith('win'):
        windir = Path(os.environ.get('WINDIR', r'C:\Windows'))
        candidates += [windir/'Fonts'/x for x in (
            'yumin.ttf', 'YuMincho.ttc', 'msmincho.ttc',
            'meiryo.ttc', 'msgothic.ttc',
        )]
    else:
        candidates += [Path(x) for x in (
            '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        )]
    for file in candidates:
        if file.is_file():
            try:
                return str(file), fitz.Font(fontfile=str(file))
            except (RuntimeError, OSError, ValueError):
                continue
    return None, fitz.Font(fontname='japan')



def find_gothic_font() -> tuple[str | None, fitz.Font, str]:
    """Prefer the locally installed Kozuka Gothic Pro R, then Japanese sans-serif."""
    candidate_paths: list[tuple[str, Path]] = []
    if sys.platform.startswith('win'):
        try:
            import winreg
            key = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts'
            windir = Path(os.environ.get('WINDIR', r'C:\Windows'))
            fonts = windir / 'Fonts'
            user_fonts = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Windows' / 'Fonts'
            entries=[]
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, key) as reg:
                        i=0
                        while True:
                            try:
                                name, filename, _ = winreg.EnumValue(reg, i)
                            except OSError:
                                break
                            i+=1
                            folded = unicodedata.normalize('NFKC', name).replace(' ', '').lower()
                            if 'kozuka' in folded and ('gothic' in folded or 'ゴシック' in folded):
                                priority=0 if ('pro' in folded and (' r' in name.lower() or 'regular' in folded)) else 1
                            elif '小塚ゴシック' in name:
                                priority=1
                            else:
                                continue
                            path=Path(str(filename))
                            for file in ([path] if path.is_absolute() else [user_fonts/path, fonts/path]):
                                if file.is_file() and file.suffix.lower() in {'.ttf','.otf','.ttc'}:
                                    entries.append((priority,name,file))
                                    break
                except OSError:
                    continue
            entries.sort(key=lambda x:x[0])
            candidate_paths.extend((name,path) for _,name,path in entries)
        except (ImportError, OSError):
            pass
        for filename in ('YuGothM.ttc', 'meiryo.ttc', 'msgothic.ttc'):
            candidate_paths.append((filename, Path(os.environ.get('WINDIR', r'C:\Windows'))/'Fonts'/filename))
    else:
        for filename in (
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        ):
            candidate_paths.append(('Noto Sans CJK', Path(filename)))
    for label, file in candidate_paths:
        if file.is_file():
            try:
                return str(file), fitz.Font(fontfile=str(file)), label
            except (RuntimeError, ValueError, OSError):
                continue
    path, font=find_jp_font()
    return path, font, '日本語代替フォント'


def _windows_kai_font_candidates() -> list[tuple[str, Path]]:
    """Installed fonts from Windows registry; no redistribution or copying of fonts.

    Prioritize the proportional AR P楷書体M used by the supplied PDF, then
    non-proportional AR楷書体M. Only files already installed by the user are read.
    """
    if not sys.platform.startswith('win'):
        return []
    try:
        import winreg
    except ImportError:
        return []
    fonts_dir = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    user_dir = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Windows' / 'Fonts'
    regkey = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts'
    entries: list[tuple[int, str, Path]] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, regkey) as reg:
                i = 0
                while True:
                    try:
                        label, filename, _ = winreg.EnumValue(reg, i)
                    except OSError:
                        break
                    i += 1
                    name = re.sub(r'\s+', '', unicodedata.normalize('NFKC', label)).casefold()
                    if '楷書体' not in name:
                        continue
                    # AR P楷書体M and AR楷書体M are the relevant Arphic faces.
                    if 'arp楷書体m' in name:
                        rank = 0
                    elif 'ar楷書体m' in name:
                        rank = 1
                    else:
                        continue
                    fp = Path(str(filename))
                    options = [fp] if fp.is_absolute() else [user_dir / fp, fonts_dir / fp]
                    for path in options:
                        if path.is_file() and path.suffix.lower() in {'.ttf','.otf','.ttc'}:
                            entries.append((rank, label, path))
                            break
        except OSError:
            continue
    entries.sort(key=lambda item: item[0])
    return [(label, path) for _, label, path in entries]


def find_kai_font(preferred_file: str = '') -> tuple[str | None, fitz.Font, str]:
    """Prefer the user's installed ARP face; use a Japanese font when absent.

    Explicit path is for cases where Windows lists the font under an unusual
    registry name. The path stays on the user's machine; it is not packaged.
    """
    if preferred_file:
        selected = Path(preferred_file)
        if selected.is_file() and selected.suffix.lower() in {'.ttf', '.otf'}:
            try:
                return str(selected), fitz.Font(fontfile=str(selected)), f'指定フォント：{selected.name}'
            except (RuntimeError, OSError, ValueError):
                pass
    for name, file in _windows_kai_font_candidates():
        try:
            return str(file), fitz.Font(fontfile=str(file)), f'自動検出：{name}'
        except (RuntimeError, OSError, ValueError):
            continue
    # Fallback: choose an actually usable Japanese typeface, not a pseudo-Kaisho.
    path, font = find_jp_font()
    return path, font, '代替書体（ARP楷書体Mは未検出）'


# The x-shift is shared by the two cards of each column. The y-shift is shared
# by the three cards in each row. Limits preserve the printable margin.
PT_PER_MM = 72 / 25.4
POSITION_LIMITS_MM = {'columns': 6.0, 'rows': 3.0}


def position_values(settings: dict | None = None) -> tuple[list[float], list[float]]:
    """Read legacy and new settings safely; malformed numbers never break printing."""
    raw = (settings or {}).get('position_mm', {})
    if not isinstance(raw, dict):
        raw = {}
    values = []
    for key, count in [('columns', 3), ('rows', 2)]:
        sequence = raw.get(key, [])
        if not isinstance(sequence, (list, tuple)):
            sequence = []
        limit = POSITION_LIMITS_MM[key]
        result = []
        for i in range(count):
            try:
                value = float(sequence[i])
                if not math.isfinite(value):
                    raise ValueError('Not finite')
            except (ValueError, TypeError, IndexError):
                value = 0.0
            result.append(max(-limit, min(limit, value)))
        values.append(result)
    return values[0], values[1]


def card_origin(col: int, row: int, settings: dict | None = None) -> tuple[float, float]:
    """Origin of a complete card, shifted independently by column/row in mm."""
    dx, dy = position_values(settings)
    x = col*CELL_W + (CELL_W-GRID_W)/2 - GRID_X + dx[col]*PT_PER_MM
    content_h = BOTTOM_BAR_END - TOP_BAR_Y
    y = row*CELL_H + (CELL_H-content_h)/2 - TOP_BAR_Y + dy[row]*PT_PER_MM
    return x, y


def crop_mark_segments(settings: dict | None = None) -> list[tuple[tuple[float,float], tuple[float,float]]]:
    """Cut guides follow adjacent card MIDPOINTS after position adjustment.

    Vertical cutting positions are midway between horizontally shifted column
    centers; the horizontal cutting position between row centers. Because the
    offsets are shared by each entire column/row, each cut remains a straight
    line from end to end and never intersects the cards (within safe limits).
    """
    inset, length = 4*PT_PER_MM, 3*PT_PER_MM
    cols, rows = position_values(settings)
    xs = [CELL_W*(i+.5)+cols[i]*PT_PER_MM for i in range(3)]
    ys = [CELL_H*(i+.5)+rows[i]*PT_PER_MM for i in range(2)]
    cuts_x = [(xs[0]+xs[1])/2, (xs[1]+xs[2])/2]
    cut_y = (ys[0]+ys[1])/2
    segments = []
    for divider in cuts_x:
        segments.append(((divider, inset), (divider, inset+length)))
        segments.append(((divider, PAGE_H-inset-length), (divider, PAGE_H-inset)))
    segments.extend([
        ((inset, cut_y), (inset+length, cut_y)),
        ((PAGE_W-inset-length, cut_y), (PAGE_W-inset, cut_y)),
    ])
    return segments


class CalendarRenderer:
    """PDF内の1枚分の共通描画ロジック。

    PDF/画面プレビューで同じコードを使うため、画面と印刷結果がズレにくい。
    同梱したロゴはフォント依存のないベクター形状として扱う。
    """
    def __init__(self):
        geometry = json.loads(KUMO_GEOMETRY.read_text(encoding='utf-8'))
        self.cloud_bounds = fitz.Rect(geometry['bounds'])
        self.cloud_paths = geometry['drawings']
        self.logo_doc = fitz.open(NAME_LOGO_PDF)
        # This clipping rectangle contains exactly the user-uploaded outlined lettering + blue bar.
        self.logo_clip = fitz.Rect(159.55, 832.08, 886.88, 898.59)
        self.font_path, self.font, self.gothic_font_name = find_gothic_font()
        self.kai_font_path, self.kai_font, self.kai_font_name = find_kai_font()
        self.kai_user_choice = ''
        self.holidays = read_holidays()

    def _ensure_font(self, settings: dict):
        desired = str(settings.get('calendar_font_file', '') or '')
        if desired == self.kai_user_choice:
            return
        self.kai_font_path, self.kai_font, self.kai_font_name = find_kai_font(desired)
        self.kai_user_choice = desired

    def close(self):
        self.logo_doc.close()

    def _text(self, page, value: str, center_x: float, baseline: float,
              size: float, color=BLACK, max_width=None, *, kai=False):
        if not value:
            return
        selected = self.kai_font if kai else self.font
        width = selected.text_length(value, fontsize=size)
        if max_width is not None and width > max_width and width > 0:
            size *= max_width/width
        width = selected.text_length(value, fontsize=size)
        page.insert_text((center_x-width/2, baseline), value,
                         fontname='kai' if kai else 'jp', fontsize=size, color=color)

    def _prepare_page(self, page):
        if self.font_path:
            page.insert_font(fontname='jp', fontfile=self.font_path)
        else:
            page.insert_font(fontname='jp', fontbuffer=self.font.buffer)
        if self.kai_font_path:
            page.insert_font(fontname='kai', fontfile=self.kai_font_path)
        else:
            page.insert_font(fontname='kai', fontbuffer=self.kai_font.buffer)

    def _cloud(self, page, gx: float, gy: float, settings: dict):
        """Draw the preprocessed background SVG as vector paths."""
        # The date grid excludes the weekday header. Center the original SVG there.
        date_top = gy + HEADER_H
        target = fitz.Rect(gx+12, date_top+9,
                           gx+GRID_W-12, gy+GRID_H-9)
        scale_x = target.width/self.cloud_bounds.width
        scale_y = target.height/self.cloud_bounds.height
        def pt(pair):
            return fitz.Point(target.x0+(pair[0]-self.cloud_bounds.x0)*scale_x,
                              target.y0+(pair[1]-self.cloud_bounds.y0)*scale_y)
        color = color_for_logo(settings)
        alpha = opacity(settings, 'logo_opacity', 22)
        for data in self.cloud_paths:
            shape = page.new_shape()
            for cmd in data['ops']:
                pts = list(map(pt, cmd['pts']))
                if cmd['cmd'] == 'c':
                    shape.draw_bezier(*pts)
                elif cmd['cmd'] == 'l':
                    shape.draw_line(*pts)
            cutout = data['fill_type'] == 'cutout'
            # White cutout maintains the vector silhouette from the supplied SVG.
            shape.finish(color=None, fill=WHITE if cutout else color,
                         fill_opacity=1. if cutout else alpha, closePath=True)
            shape.commit()

    def _card(self, page, x: float, y: float, year: int, month: int,
              rests: set[int], settings: dict):
        """1枚の完成デザインを描画。編集・設定プレビュー・6面PDFで共通使用。"""
        gx, gy = x+GRID_X, y+GRID_Y
        page.draw_rect(fitz.Rect(gx, y+TOP_BAR_Y, gx+GRID_W, y+24.3),
                       color=None, fill=NAVY)
        self._cloud(page, gx, gy, settings)
        first = (dt.date(year, month, 1).weekday()+1) % 7
        count = calendar.monthrange(year, month)[1]
        weeks = max(4, (first+count+6)//7)
        cw = GRID_W/7
        row_h = (GRID_H-HEADER_H)/weeks
        # Draw semi-transparent rest-day cells AFTER logo, so the SVG shows through.
        for day in sorted(rests):
            if not 1 <= day <= count:
                continue
            k = first+day-1
            col, row = k%7, k//7
            rect = fitz.Rect(gx+col*cw, gy+HEADER_H+row*row_h,
                             gx+(col+1)*cw, gy+HEADER_H+(row+1)*row_h)
            page.draw_rect(rect, color=None, fill=PINK,
                           fill_opacity=opacity(settings, 'closure_opacity', 38))

        # Pale weekday header: visually shorter than date rows and clearly divided.
        page.draw_rect(fitz.Rect(gx, gy, gx+GRID_W, gy+HEADER_H),
                       color=None, fill=(0.947, 0.963, 0.989), fill_opacity=0.92)
        page.draw_rect(fitz.Rect(gx, gy, gx+GRID_W, gy+GRID_H),
                       color=LINE, width=.85)
        for c in range(1, 7):
            xx = gx+c*cw
            page.draw_line((xx, gy), (xx, gy+GRID_H), color=LINE, width=.45)
        page.draw_line((gx, gy+HEADER_H), (gx+GRID_W, gy+HEADER_H),
                       color=(0.15, 0.26, 0.45), width=1.5)
        for r in range(1, weeks):
            yy = gy+HEADER_H+r*row_h
            page.draw_line((gx, yy), (gx+GRID_W, yy), color=LINE, width=.5)

        label = str(month)
        self._text(page, f'{label}月 お休みのお知らせ',
                   gx+GRID_W/2, y+43.5, 17, NAVY, GRID_W-6, kai=True)
        for c, weekday in enumerate('日月火水木金土'):
            col = MAGENTA if c == 0 else SATURDAY if c == 6 else BLACK
            self._text(page, weekday, gx+(c+.5)*cw, gy+18.1, 15.5, col, kai=True)
        for day in range(1, count+1):
            k = first+day-1
            c, r = k%7, k//7
            # 日付の下に祝日名を追加。小さいセルでも右端からはみ出さないよう
            # 描画時にフォント幅を測定し、セル幅に収まるまで自動的に縮小する。
            holiday_name = self.holidays.get(dt.date(year, month, day).isoformat(), '')
            col = MAGENTA if c == 0 or holiday_name else SATURDAY if c == 6 else BLACK
            self._text(page, str(day), gx+(c+.5)*cw,
                       gy+HEADER_H+row_h*(r+.5)+5.5, 17, col, kai=True)
            if holiday_name:
                small = 5.3 if weeks < 6 else 4.8
                max_width = cw - 2.7
                measured = self.font.text_length(holiday_name, fontsize=small)
                if measured > max_width:
                    small *= max_width/measured
                # セル右端より1.3pt内側、セル下端より2pt上に配置する。
                right = gx + (c+1)*cw - 1.3
                bottom = gy + HEADER_H + (r+1)*row_h - 2.1
                text_width = self.font.text_length(holiday_name, fontsize=small)
                page.insert_text((right-text_width, bottom), holiday_name,
                                 fontname='jp', fontsize=small, color=MAGENTA)

        # Larger legend swatch: pink translucent fill + navy border for visibility.
        # Match the visual height of the adjacent 8.4pt "休業日" label.
        # A light outline keeps even the low-opacity swatch recognizable.
        page.draw_rect(fitz.Rect(gx+GRID_W-40.5, y+246.0,
                                gx+GRID_W-33.1, y+253.4),
                       fill=PINK, fill_opacity=opacity(settings, 'closure_opacity', 38),
                       color=LINE, width=.65)
        self._text(page, '休業日', gx+GRID_W-15.3, y+253.1, 8.4, NAVY)
        notice = f"{settings.get('notice','')}  {settings.get('phone','')}".strip()
        self._text(page, notice, gx+GRID_W/2, y+262.9, 8.1, NAVY, GRID_W-1)

        # Outlined SVG logo, no replacement font. The PDF asset is lossless vector.
        rect = fitz.Rect(gx, y+BOTTOM_BAR_Y, gx+GRID_W, y+BOTTOM_BAR_END)
        page.show_pdf_page(rect, self.logo_doc, 0,
                           clip=self.logo_clip, keep_proportion=False,
                           overlay=True)

    def make_document(self, year: int, month: int,
                      rests: list[int], settings: dict,
                      single_card: bool = False) -> fitz.Document:
        """編集/設定画面は1枚、印刷はA4横の同一デザイン6枚を生成する。"""
        self._ensure_font(settings)
        doc = fitz.open()
        if single_card:
            page = doc.new_page(width=CELL_W, height=CELL_H)
            self._prepare_page(page)
            # Editing always uses the unchanged single-card page origin.
            x, y = card_origin(0, 0)
            self._card(page, x, y, year, month, set(rests), settings)
        else:
            page = doc.new_page(width=PAGE_W, height=PAGE_H)
            self._prepare_page(page)
            for row in range(2):
                for col in range(3):
                    x, y = card_origin(col, row, settings)
                    self._card(page, x, y, year, month, set(rests), settings)
            if settings.get('cut_guides', True):
                for start, end in crop_mark_segments(settings):
                    page.draw_line(start, end, color=(.50, .50, .50), width=.5)
        return doc


def export_pdf(filename: str | Path, year: int, month: int,
               rests: list[int], settings: dict, renderer: CalendarRenderer | None = None):
    """CLIやテストから呼び出せるPDF出力関数。フォントの部分埋め込みを試みる。"""
    owned = renderer is None
    renderer = renderer or CalendarRenderer()
    try:
        with renderer.make_document(year, month, rests, settings) as doc:
            try:
                doc.subset_fonts(fallback=True)
            except (RuntimeError, ValueError):
                pass
            doc.save(str(filename), garbage=4, deflate=True, deflate_fonts=True)
    finally:
        if owned:
            renderer.close()


def launch_app():
    """Windows向けTkinter UIを起動。PDF処理関数はGUIと分離してテスト可能。"""
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
    from PIL import Image, ImageTk

    # 操作部品の機能（押す／ドラッグ／入力する）を色・輪郭・カーソルで区別する。
    UI = {'bg': '#F3F7FD', 'surface': '#FFFFFF', 'ink': '#182C4E',
          'muted': '#667A94', 'primary': '#1765C7', 'hover': '#114F9E',
          'soft': '#E1EEFF', 'line': '#BDD0E7', 'canvas': '#EAF0F8',
          'accent': '#0F62C9', 'thumb': '#FFFFFF', 'track': '#B7CBE3'}

    def install_theme(root):
        root.configure(bg=UI['bg'])
        style = ttk.Style(root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('TFrame', background=UI['bg'])
        style.configure('Surface.TFrame', background=UI['surface'])
        style.configure('TLabel', background=UI['bg'], foreground=UI['ink'],
                        font=('Yu Gothic UI', 10))
        style.configure('Surface.TLabel', background=UI['surface'], foreground=UI['ink'])
        style.configure('Muted.TLabel', background=UI['bg'], foreground=UI['muted'],
                        font=('Yu Gothic UI', 9))
        style.configure('Title.TLabel', background=UI['bg'], foreground=UI['ink'],
                        font=('Yu Gothic UI', 18, 'bold'))
        style.configure('Heading.TLabel', background=UI['bg'], foreground=UI['ink'],
                        font=('Yu Gothic UI', 12, 'bold'))
        # clam テーマで境界線・立体感・hover/pressedを明示する。
        # 見出しや通常のラベルにボタンと同じ見た目を使わない。
        def button_style(name, *, bg, fg, active, pad=(13, 8), bold=False):
            style.configure(name, font=('Yu Gothic UI', 10, 'bold' if bold else 'normal'),
                            padding=pad, background=bg, foreground=fg,
                            relief='raised', borderwidth=2, bordercolor='#92B1D3',
                            lightcolor='#FFFFFF', darkcolor='#8EADCF',
                            focuscolor=UI['primary'])
            style.map(name,
                      background=[('pressed', '#BDD7F5'), ('active', active)],
                      relief=[('pressed', 'sunken'), ('active', 'raised')],
                      foreground=[('disabled', UI['muted']), ('active', fg)])
        button_style('TButton', bg='#FFFFFF', fg=UI['ink'], active='#EEF5FF')
        button_style('Primary.TButton', bg=UI['primary'], fg='#FFFFFF',
                     active='#0F529F', pad=(17, 9), bold=True)
        button_style('Soft.TButton', bg='#E3EFFD', fg='#194D87',
                     active='#D1E5FF', bold=True)
        button_style('Action.TButton', bg='#D9EAFF', fg='#073F84',
                     active='#C0DCFF', bold=True)
        button_style('Arrow.TButton', bg='#FFFFFF', fg=UI['primary'],
                     active='#DBECFF', pad=(8, 6), bold=True)
        button_style('Mini.TButton', bg='#FFFFFF', fg='#184F91',
                     active='#CFE6FF', pad=(6, 4), bold=True)
        style.configure('Section.TLabel', font=('Yu Gothic UI', 11, 'bold'),
                        foreground='#174E90', background=UI['bg'])
        style.configure('Help.TLabel', font=('Yu Gothic UI', 9),
                        foreground=UI['muted'], background=UI['bg'])
        # 操作できる部品ではポインターを変更。無効化状態は別途ttkが表現する。
        root.option_add('*TButton.cursor', 'hand2')
        style.configure('TEntry', padding=(7, 6), fieldbackground='#FFFFFF',
                        foreground=UI['ink'], bordercolor='#96B1D5', borderwidth=2,
                        relief='sunken')
        style.map('TEntry', bordercolor=[('focus', UI['primary'])])
        style.configure('TSeparator', background=UI['line'])
        style.configure('TCheckbutton', font=('Yu Gothic UI', 10),
                        foreground=UI['ink'], background=UI['bg'])
        style.configure('Vertical.TScrollbar', background='#D3E1F3')
        return style

    class ModernSlider(tk.Frame):
        """目で『つまみをドラッグできる』と分かる、キーボード対応のスライダー。

        Tk標準ScaleのOS依存のつまみ色を避ける。外部GUIライブラリは不要。
        実際の設定値はIntVarを共有し、既存の即時プレビュー更新にも連動する。
        """
        def __init__(self, master, variable, *, width=148):
            super().__init__(master, bg=UI['bg'])
            self.variable = variable
            self.scale_width = width
            self.trough = tk.Canvas(self, width=width, height=32,
                                    bg=UI['bg'], highlightthickness=0,
                                    takefocus=1, cursor='hand2')
            self.trough.pack(side='left', fill='x', expand=True)
            self.value = tk.Label(self, width=6, anchor='center', bg='#FFFFFF',
                                  fg=UI['ink'], relief='solid', bd=1,
                                  font=('Yu Gothic UI', 10, 'bold'))
            self.value.pack(side='left', padx=(9, 0), ipadx=2, ipady=3)
            self.trough.bind('<Button-1>', self._mouse)
            self.trough.bind('<B1-Motion>', self._mouse)
            self.trough.bind('<Left>', lambda e: self._step(-1))
            self.trough.bind('<Right>', lambda e: self._step(1))
            self.trough.bind('<Home>', lambda e: self._set(0))
            self.trough.bind('<End>', lambda e: self._set(100))
            self.trough.bind('<Configure>', lambda e: self._paint())
            variable.trace_add('write', lambda *_: self._paint())
            self._paint()

        def _set(self, value):
            self.variable.set(max(0, min(100, int(value))))

        def _step(self, amount):
            self._set(self.variable.get()+amount)
            return 'break'

        def _mouse(self, event):
            self.trough.focus_set()
            width = max(80, self.trough.winfo_width())
            left, right = 13, width-13
            self._set(round((event.x-left)/(right-left)*100))

        def _paint(self):
            c = self.trough
            if not c.winfo_exists():
                return
            c.delete('all')
            width = max(80, c.winfo_width())
            left, right, cy = 13, width-13, 16
            fraction = max(0, min(100, int(self.variable.get())))/100
            x = left+(right-left)*fraction
            # 太めのトラックと進捗色。つまみは白＋青の二重輪郭と影で目立たせる。
            c.create_line(left, cy, right, cy, fill=UI['track'], width=8,
                          capstyle='round')
            c.create_line(left, cy, x, cy, fill=UI['primary'], width=8,
                          capstyle='round')
            c.create_oval(x-10, cy-8, x+12, cy+13, fill='#A9BEDA', outline='')
            c.create_oval(x-11, cy-11, x+11, cy+11, fill='#FFFFFF',
                          outline=UI['primary'], width=2)
            c.create_oval(x-3, cy-3, x+3, cy+3, fill=UI['primary'], outline='')
            if str(c.focus_get()) == str(c):
                c.create_rectangle(1,1,width-1,31, outline='#85AFE8', dash=(2,2))
            self.value.configure(text=f'{int(self.variable.get())}%')

    class App:
        def __init__(self):
            self.win = tk.Tk()
            install_theme(self.win)
            self.win.title('休業日カレンダー作成ツール')
            # Windowsの表示倍率150%でも画面下へはみ出しにくい初期サイズ。
            screen_w, screen_h = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            self.win.geometry(f'{min(930, max(610, screen_w-90))}x'
                              f'{min(790, max(480, screen_h-110))}')
            self.win.minsize(590, 470)
            self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
            self.closed = load_json(CLOSURES_FILE, dict(DEFAULT_RESTS))
            now = dt.date.today()
            self.year, self.month = now.year, now.month
            self.renderer = CalendarRenderer()
            self.image_handle = None
            self.open_image = None
            self.closed_image = None
            self.display_image = None
            self.image_xy = (0, 0)
            self.preview_scale = 1
            self.pending_redraw = None
            self.fetching = False
            self._layout()
            if DATA_DIR_WARNING:
                self.status.set(DATA_DIR_WARNING)
            elif DATA_IS_FALLBACK:
                self.status.set(f'アプリの場所に保存できないため、ユーザー領域に保存: {DATA}')
            self.schedule_refresh()
            self.win.after(350, self._auto_update_holidays)
            self.win.protocol('WM_DELETE_WINDOW', self._close)

        def _close(self):
            self.renderer.close()
            self.win.destroy()

        def _layout(self):
            """Modern single-card editor: complete PDF graphics, not a simplified grid."""
            header = ttk.Frame(self.win, padding=(20, 13, 20, 9))
            header.pack(fill='x')
            titleline = ttk.Frame(header)
            titleline.pack(fill='x')
            ttk.Label(titleline, text='▦  休業日カレンダー作成ツール',
                      style='Title.TLabel').pack(side='left')
            ttk.Label(titleline, text='日付をクリックして休業日を編集',
                      style='Muted.TLabel').pack(side='right', pady=7)

            toolbar = ttk.Frame(self.win, padding=(20, 4, 20, 9))
            toolbar.pack(fill='x')
            ttk.Button(toolbar, text='◀', style='Arrow.TButton',
                       command=lambda: self.move(-1)).pack(side='left')
            self.month_label = ttk.Label(toolbar, font=('Yu Gothic UI', 17, 'bold'),
                                         width=12, anchor='center')
            self.month_label.pack(side='left', padx=6)
            ttk.Button(toolbar, text='▶', style='Arrow.TButton',
                       command=lambda: self.move(1)).pack(side='left')
            ttk.Button(toolbar, text='PDF出力 →', style='Primary.TButton',
                       command=self.export).pack(side='right', padx=(8, 0))
            ttk.Button(toolbar, text='⚙ 設定', style='Soft.TButton',
                       command=self.settings_dialog).pack(side='right', padx=(8, 0))
            ttk.Button(toolbar, text='↔ 位置調整', style='Soft.TButton',
                       command=self.position_dialog).pack(side='right')
            ttk.Separator(self.win).pack(fill='x')
            body = ttk.Frame(self.win, padding=(17, 13, 17, 10))
            body.pack(fill='both', expand=True)
            self.canvas = tk.Canvas(body, bg=UI['canvas'], cursor='hand2',
                                    highlightthickness=0, bd=0)
            self.canvas.pack(fill='both', expand=True)
            self.canvas.bind('<Button-1>', self.on_click)
            self.canvas.bind('<Configure>', lambda event: self.schedule_refresh())
            self.status = tk.StringVar(value=(f'保存先: {DATA}' if DATA_IS_FALLBACK
                                              else '✓  編集内容は月別に自動保存されます'))
            footer = ttk.Frame(self.win, padding=(18, 8, 18, 10))
            footer.pack(fill='x')
            ttk.Label(footer, textvariable=self.status, style='Muted.TLabel').pack(side='left')
            ttk.Label(footer, text='A4横  •  3列 × 2段  •  高精細PDF',
                      style='Muted.TLabel').pack(side='right')

        def key(self):
            return f'{self.year:04d}-{self.month:02d}'

        def move(self, delta: int):
            index = self.year*12+self.month-1+delta
            self.year, zero_month = divmod(index, 12)
            self.month = zero_month+1
            self.schedule_refresh()

        def schedule_refresh(self):
            if self.pending_redraw:
                self.win.after_cancel(self.pending_redraw)
            self.pending_redraw = self.win.after(180, self.refresh)

        def _day_box_px(self, day: int) -> tuple[int, int, int, int]:
            """Exact cell crop in the two cached, pixel-identical card renders."""
            from math import ceil
            scale = self.preview_scale
            count = calendar.monthrange(self.year, self.month)[1]
            if day < 1 or day > count:
                raise ValueError('Invalid day')
            first = (dt.date(self.year, self.month, 1).weekday()+1) % 7
            weeks = max(4, (first+count+6)//7)
            idx=first+day-1
            col,row=idx%7,idx//7
            x,y=card_origin(0,0)
            gx,gy=x+GRID_X,y+GRID_Y
            cw=GRID_W/7
            rh=(GRID_H-HEADER_H)/weeks
            x0=ceil((gx+col*cw)*scale)
            x1=ceil((gx+(col+1)*cw)*scale)
            y0=ceil((gy+HEADER_H+row*rh)*scale)
            y1=ceil((gy+HEADER_H+(row+1)*rh)*scale)
            return (x0, y0, x1, y1)

        def _compose_image(self):
            """No PDF work: paste only the selected closed cells from cached images."""
            result=self.open_image.copy()
            for day in self.closed.get(self.key(), []):
                if 1 <= day <= calendar.monthrange(self.year,self.month)[1]:
                    rect=self._day_box_px(day)
                    result.paste(self.closed_image.crop(rect),rect[:2])
            return result

        def refresh(self):
            self.pending_redraw = None
            self.month_label.configure(text=f'{self.year}年{self.month}月')
            self.canvas.update_idletasks()
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            if w < 30 or h < 30:
                return
            self.preview_scale=min((w-38)/CELL_W, (h-38)/CELL_H, 2.7)
            # Each PDF includes exactly one card with all original artwork,
            # outlined store logo, typography and SVG cloud; no simplified UI.
            count=calendar.monthrange(self.year,self.month)[1]
            pix_images=[]
            for rests in ([], list(range(1,count+1))):
                with self.renderer.make_document(self.year,self.month,rests,self.settings,
                                                  single_card=True) as doc:
                    pix=doc[0].get_pixmap(matrix=fitz.Matrix(self.preview_scale,
                                                            self.preview_scale),alpha=False)
                    pix_images.append(Image.frombytes('RGB',(pix.width,pix.height),pix.samples))
            self.open_image, self.closed_image = pix_images
            self.display_image=self._compose_image()
            self.image_handle=ImageTk.PhotoImage(self.display_image)
            self.image_xy=((w-self.display_image.width)//2,(h-self.display_image.height)//2)
            self.canvas.delete('all')
            self.canvas.create_image(*self.image_xy,image=self.image_handle,anchor='nw')

        def on_click(self, event):
            """クリック座標から年月日を逆算し、そのセルのみ再描画して自動保存。"""
            if self.preview_scale <= 0 or self.display_image is None:
                return
            x=(event.x-self.image_xy[0])/self.preview_scale
            y=(event.y-self.image_xy[1])/self.preview_scale
            origin_x,origin_y=card_origin(0,0)
            gx,gy=origin_x+GRID_X,origin_y+GRID_Y
            if not (gx <= x < gx+GRID_W and gy+HEADER_H <= y < gy+GRID_H):
                return
            count=calendar.monthrange(self.year,self.month)[1]
            first=(dt.date(self.year,self.month,1).weekday()+1)%7
            weeks=max(4,(first+count+6)//7)
            row=int((y-gy-HEADER_H)/((GRID_H-HEADER_H)/weeks))
            col=int((x-gx)/(GRID_W/7))
            day=row*7+col-first+1
            if not 1 <= day <= count:
                return
            new_rests=set(self.closed.get(self.key(),[]))
            new_rests.symmetric_difference_update((day,))
            new_closed=dict(self.closed)
            new_closed[self.key()]=sorted(new_rests)
            try:
                save_json(CLOSURES_FILE,new_closed)
            except OSError as err:
                messagebox.showerror('保存エラー',str(err));return
            self.closed=new_closed
            # Immediate cell replacement: no PDF regeneration or full-screen redraw.
            rect=self._day_box_px(day)
            source=self.closed_image if day in new_rests else self.open_image
            self.display_image.paste(source.crop(rect),rect[:2])
            self.image_handle.paste(self.display_image)
            self.status.set(f'{self.key()}  {day}日の休業日設定を保存しました。')

        def position_dialog(self, parent=None, on_saved=None):
            """Graphical position editor: preview + five controls in ONE window.

            Row y positions move three cards together; column x positions move
            two cards together. Values are in millimetres; click +/-0.1 mm.
            """
            parent = parent or self.win
            top = tk.Toplevel(parent)
            install_theme(top)
            top.title('PDF出力の位置調整')
            screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
            top.geometry(f'{min(1240, max(770, screen_w-80))}x'
                         f'{min(810, max(520, screen_h-100))}')
            top.minsize(740, 480)
            top.transient(parent)
            top.grab_set()

            work = dict(self.settings)
            cols, rows = position_values(work)
            values = [tk.StringVar(value=f'{n:+.1f}') for n in cols+rows]
            layout = ttk.Frame(top, padding=(16, 10, 16, 8))
            layout.pack(fill='both', expand=True)
            layout.columnconfigure(0, weight=1)
            layout.rowconfigure(2, weight=1)
            ttk.Label(layout, text='↔  PDF出力の位置調整',
                      style='Title.TLabel').grid(row=0, column=0, sticky='w')
            ttk.Label(layout, text='プレビューを見ながら列・段を個別に調整します（mm単位）。',
                      style='Muted.TLabel').grid(row=1, column=0,
                                                   sticky='w', pady=(1, 6))
            content = ttk.Frame(layout)
            content.grid(row=2, column=0, sticky='nsew')
            content.columnconfigure(0, weight=1)
            content.columnconfigure(1, weight=0)
            content.rowconfigure(1, weight=1)

            # One horizontal strip, with equal-width groups aligned to the three
            # columns of the PDF preview. Unlike Canvas window items, ordinary
            # Frame/grid controls remain correctly positioned at high DPI.
            top_strip = ttk.Frame(content, height=75)
            top_strip.grid(row=0, column=0, sticky='ew', padx=(0, 10), pady=(0, 5))
            top_strip.grid_propagate(False)
            columns_bar = ttk.Frame(top_strip)
            columns_bar.place(relx=.5, y=0, anchor='n', width=630)
            for ci, title in enumerate(('左列（2枚）', '中央列（2枚）', '右列（2枚）')):
                columns_bar.columnconfigure(ci, weight=1, uniform='columns')
                group = ttk.Frame(columns_bar, padding=(2, 0))
                group.grid(row=0, column=ci, sticky='n')
                ttk.Label(group, text=title,
                          font=('Yu Gothic UI', 10, 'bold')).pack()
                controls = ttk.Frame(group)
                controls.pack()
                ttk.Button(controls, text='◀', style='Mini.TButton', width=2,
                           command=lambda i=ci: step(i, -1)).pack(side='left')
                entry = ttk.Entry(controls, textvariable=values[ci], width=6,
                                  justify='center')
                entry.pack(side='left', padx=3)
                ttk.Button(controls, text='▶', style='Mini.TButton', width=2,
                           command=lambda i=ci: step(i, 1)).pack(side='left')
                ttk.Label(group, text='mm', style='Muted.TLabel').pack()
                entry.bind('<Return>', lambda e: apply_values())
                entry.bind('<FocusOut>', lambda e: apply_values())

            canvas = tk.Canvas(content, bg=UI['canvas'], highlightthickness=0)
            canvas.grid(row=1, column=0, sticky='nsew', padx=(0, 10))
            side = ttk.Frame(content, padding=(9, 5, 0, 5))
            side.grid(row=0, column=1, rowspan=2, sticky='nsew')
            ttk.Label(side, text='段の上下位置', style='Heading.TLabel').pack(pady=(2, 4))
            for ri, title in enumerate(('上段（3枚）', '下段（3枚）')):
                group = ttk.Frame(side, padding=(4, 3))
                group.pack(fill='x', pady=(2, 6))
                ttk.Label(group, text=title,
                          font=('Yu Gothic UI', 10, 'bold')).pack(pady=(0, 2))
                idx = 3+ri
                ttk.Button(group, text='▲ 上へ', style='Mini.TButton', width=7,
                           command=lambda i=idx: step(i, -1)).pack(fill='x')
                entry = ttk.Entry(group, textvariable=values[idx], width=8,
                                  justify='center')
                entry.pack(pady=5)
                ttk.Label(group, text='mm', style='Muted.TLabel').pack()
                ttk.Button(group, text='▼ 下へ', style='Mini.TButton', width=7,
                           command=lambda i=idx: step(i, 1)).pack(fill='x', pady=(5, 0))
                entry.bind('<Return>', lambda e: apply_values())
                entry.bind('<FocusOut>', lambda e: apply_values())

            foot = ttk.Frame(layout, padding=(0, 7, 0, 0))
            foot.grid(row=3, column=0, sticky='ew')
            ttk.Label(foot, text='移動量は0.1mm刻み。裁断マークも自動で再配置されます。',
                      style='Muted.TLabel').pack(side='left')
            ttk.Button(foot, text='キャンセル', command=lambda: close(False)).pack(side='right')
            ttk.Button(foot, text='設定を保存', style='Primary.TButton',
                       command=lambda: close(True)).pack(side='right', padx=8)
            ttk.Button(foot, text='位置をリセット', style='Soft.TButton',
                       command=lambda: reset()).pack(side='right')
            current = {'job': None, 'image': None, 'error': None}

            def normalize(index, raw):
                try:
                    val = float(raw)
                    if not math.isfinite(val):
                        raise ValueError()
                except (ValueError, TypeError):
                    return None
                cap = POSITION_LIMITS_MM['columns' if index < 3 else 'rows']
                return round(max(-cap, min(cap, val)), 1)

            def apply_values(show_errors=False):
                new = []
                for idx, var in enumerate(values):
                    parsed = normalize(idx, var.get())
                    if parsed is None:
                        if show_errors:
                            messagebox.showwarning('数値の確認',
                                '位置は数値（mm）で指定してください。', parent=top)
                        return False
                    new.append(parsed)
                for v, n in zip(values, new):
                    if v.get() != f'{n:+.1f}':
                        v.set(f'{n:+.1f}')
                work['position_mm'] = {'columns': new[:3], 'rows': new[3:]}
                schedule()
                return True

            def step(index, direction):
                value = normalize(index, values[index].get())
                if value is None:
                    value = 0.0
                cap = POSITION_LIMITS_MM['columns' if index < 3 else 'rows']
                value = max(-cap, min(cap, round(value+direction*0.1, 1)))
                values[index].set(f'{value:+.1f}')
                apply_values()

            def reset():
                for v in values:
                    v.set('+0.0')
                apply_values()

            def redraw():
                current['job'] = None
                if not top.winfo_exists():
                    return
                w, h = canvas.winfo_width(), canvas.winfo_height()
                if w < 30 or h < 30:
                    return
                scale = min((w-32)/PAGE_W, (h-32)/PAGE_H, 2.0)
                try:
                    with self.renderer.make_document(
                        self.year, self.month, self.closed.get(self.key(), []), work
                    ) as pdf:
                        pix = pdf[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                    current['image'] = ImageTk.PhotoImage(image)
                    canvas.delete('all')
                    x = (w-image.width)//2
                    y = (h-image.height)//2
                    canvas.create_rectangle(x-2, y-2, x+image.width+2,
                        y+image.height+2, fill='#CAD8E8', outline='')
                    canvas.create_image(x, y, image=current['image'], anchor='nw')
                    # Keep button group centers aligned to preview columns; on
                    # very small screens allow a minimum usable control width.
                    available = max(1, top_strip.winfo_width()-8)
                    control_width = min(available, max(420, image.width))
                    columns_bar.place_configure(width=control_width)
                except Exception as exc:
                    current['error'] = exc
                    self.status.set(f'位置調整プレビューエラー: {exc}')

            def schedule(event=None):
                if current['job'] is not None:
                    top.after_cancel(current['job'])
                current['job'] = top.after(180, redraw)

            def close(save):
                if save:
                    if not apply_values(show_errors=True):
                        return
                    try:
                        save_json(SETTINGS_FILE, work)
                    except OSError as exc:
                        messagebox.showerror('保存エラー', str(exc), parent=top)
                        return
                    self.settings = dict(work)
                    self.status.set('PDF出力の位置調整を保存しました。')
                    if on_saved:
                        on_saved()
                if current['job'] is not None:
                    top.after_cancel(current['job'])
                top.destroy()
                if parent is not self.win and parent.winfo_exists():
                    parent.grab_set()

            canvas.bind('<Configure>', schedule)
            top_strip.bind('<Configure>', schedule)
            top.protocol('WM_DELETE_WINDOW', lambda: close(False))
            work['position_mm'] = {'columns': cols[:], 'rows': rows[:]}
            schedule()

        def export(self):
            """Show the exact 3x2 A4 output before asking for a PDF filename."""
            top=tk.Toplevel(self.win)
            install_theme(top)
            top.title('PDF出力プレビュー（A4横・6枚）')
            # プレビューも小型ディスプレイや高DPIに合わせて初期サイズを調整する。
            screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
            top.geometry(f'{min(1040, max(610, screen_w-90))}x'
                         f'{min(740, max(440, screen_h-120))}')
            top.minsize(580, 400)
            top.transient(self.win)
            top.grab_set()
            page_area=ttk.Frame(top,padding=10)
            page_area.pack(fill='both',expand=True)
            ttk.Label(page_area,text='▦  A4横・6枚配置の印刷プレビュー', style='Heading.TLabel').pack(anchor='w',pady=(0,6))
            canvas=tk.Canvas(page_area,bg=UI['canvas'],highlightthickness=0)
            canvas.pack(fill='both',expand=True)
            bar=ttk.Frame(top,padding=(12,4,12,12))
            bar.pack(fill='x')
            current={'doc':None,'image':None,'job':None}
            try:
                current['doc']=self.renderer.make_document(
                    self.year,self.month,self.closed.get(self.key(),[]),self.settings)
            except Exception as err:
                top.destroy()
                messagebox.showerror('PDFプレビューエラー',str(err),parent=self.win)
                return
            def redraw():
                current['job']=None
                w,h=canvas.winfo_width(),canvas.winfo_height()
                if w<30 or h<30:return
                scale=min((w-20)/PAGE_W,(h-20)/PAGE_H,1.9)
                pix=current['doc'][0].get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
                pil=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                current['image']=ImageTk.PhotoImage(pil)
                canvas.delete('all')
                canvas.create_image((w-pil.width)//2,(h-pil.height)//2,
                                    image=current['image'],anchor='nw')
            def schedule(event=None):
                if current['job'] is not None:top.after_cancel(current['job'])
                current['job']=top.after(150,redraw)
            canvas.bind('<Configure>',schedule)
            def rebuild_preview():
                try:
                    replacement = self.renderer.make_document(
                        self.year, self.month, self.closed.get(self.key(), []), self.settings)
                except Exception as exc:
                    messagebox.showerror('プレビュー更新失敗', str(exc), parent=top)
                    return
                old = current['doc']
                current['doc'] = replacement
                if old is not None:
                    old.close()
                schedule()
            def close_preview():
                if current['job'] is not None:top.after_cancel(current['job'])
                if current['doc'] is not None:current['doc'].close()
                top.destroy()
            def save_preview():
                path=filedialog.asksaveasfilename(
                    parent=top,title='PDF出力',defaultextension='.pdf',
                    initialfile=f'休業日カレンダー_{self.key()}.pdf',
                    filetypes=[('PDFファイル','*.pdf')])
                if not path:return
                try:
                    doc=current['doc']
                    try:
                        doc.subset_fonts(fallback=True)
                    except (RuntimeError,ValueError):
                        pass
                    doc.save(path,garbage=4,deflate=True,deflate_fonts=True)
                except Exception as err:
                    messagebox.showerror('PDF出力エラー',str(err),parent=top)
                    return
                close_preview()
                self.status.set(f'PDF出力：{path}')
                messagebox.showinfo('PDF出力完了',f'「{Path(path).name}」を出力しました。',
                                    parent=self.win)
            ttk.Button(bar,text='キャンセル',command=close_preview).pack(side='right')
            ttk.Button(bar,text='PDFを保存 →',style='Primary.TButton',
                       command=save_preview).pack(side='right',padx=(0,8))
            ttk.Button(bar,text='位置調整 ↔',style='Soft.TButton',
                       command=lambda: self.position_dialog(top, rebuild_preview)).pack(side='left')
            top.protocol('WM_DELETE_WINDOW',close_preview)
            schedule()

        def _auto_update_holidays(self):
            if HOLIDAYS_FILE.exists():
                self.status.set('保存済み祝日CSVを読み込みました。')
            if holiday_update_due():
                self.update_holidays(silent=True)

        def update_holidays(self, silent=False):
            """公式CSVの取得は別スレッドで実行し、操作画面を固めない。"""
            if self.fetching:
                return
            self.fetching = True
            if not silent:
                self.status.set('内閣府の祝日データを取得しています...')
            def worker():
                try:
                    data = download_holidays()
                except Exception as err:
                    msg = str(err)
                    try:
                        self.win.after(0, lambda: self._holiday_failed(msg, silent))
                    except RuntimeError:  # Window may have closed during download.
                        pass
                else:
                    try:
                        self.win.after(0, lambda: self._holiday_done(data))
                    except RuntimeError:
                        pass
            threading.Thread(target=worker, daemon=True).start()

        def _holiday_failed(self, reason, silent):
            self.fetching = False
            if not silent:
                self.status.set('祝日更新失敗。保存済みデータを使用：' + reason)
            elif not HOLIDAYS_FILE.exists():
                self.status.set('祝日CSV未取得。2026～2027年の内蔵データを使用中です。ネット接続後に更新してください。')

        def _holiday_done(self, records):
            self.fetching = False
            self.renderer.holidays = records
            self.status.set(f'内閣府の祝日データを更新しました（{len(records)}件）。')
            self.schedule_refresh()

        def settings_dialog(self):
            """小さい画面・高DPI環境でも保存ボタンが見える設定画面。

            設定項目は左のスクロール領域へ、操作ボタンはウィンドウ下部に固定する。
            プレビューは右側の利用可能サイズに合わせて描画する。
            """
            top = tk.Toplevel(self.win)
            install_theme(top)
            top.title('設定 - 背景ロゴ・印刷')
            screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
            dialog_w = min(1030, max(660, screen_w - 90))
            dialog_h = min(770, max(410, screen_h - 120))
            top.geometry(f'{dialog_w}x{dialog_h}')
            top.minsize(610, 370)
            top.transient(self.win)
            top.grab_set()
            original = dict(self.settings)
            work = dict(original)

            # 先に下部の固定ボタン領域を確保。画面高さが小さくても隠れない。
            actions = ttk.Frame(top, padding=(12, 8, 12, 10))
            actions.pack(side='bottom', fill='x')
            ttk.Separator(top).pack(side='bottom', fill='x')
            title_row = ttk.Frame(top, padding=(17, 10, 17, 6))
            title_row.pack(side='top', fill='x')
            ttk.Label(title_row, text='⚙  表示・印刷設定',
                      style='Title.TLabel').pack(side='left')
            panel = ttk.Frame(top, padding=(12, 10))
            panel.pack(side='top', fill='both', expand=True)
            panel.columnconfigure(0, weight=0)
            panel.columnconfigure(1, weight=1)
            panel.rowconfigure(0, weight=1)

            left_area = ttk.Frame(panel, width=380)
            left_area.grid(row=0, column=0, sticky='ns', padx=(0, 12))
            left_area.grid_propagate(False)
            left_area.columnconfigure(0, weight=1)
            left_area.rowconfigure(0, weight=1)

            # Scrollbar付きCanvasの中に通常のFrameを配置する。スライダー等も入る。
            left_canvas = tk.Canvas(left_area, highlightthickness=0,
                                    borderwidth=0, width=356, bg=UI['bg'])
            left_canvas.grid(row=0, column=0, sticky='nsew')
            scroll = ttk.Scrollbar(left_area, orient='vertical', command=left_canvas.yview)
            scroll.grid(row=0, column=1, sticky='ns')
            left_canvas.configure(yscrollcommand=scroll.set)
            left = ttk.Frame(left_canvas, padding=(3, 1, 10, 10))
            window_id = left_canvas.create_window((0, 0), window=left, anchor='nw')
            left.bind('<Configure>', lambda e: left_canvas.configure(
                scrollregion=left_canvas.bbox('all')))
            left_canvas.bind('<Configure>', lambda e: left_canvas.itemconfigure(
                window_id, width=e.width))

            right = ttk.Frame(panel)
            right.grid(row=0, column=1, sticky='nsew')
            right.rowconfigure(2, weight=1)
            right.columnconfigure(0, weight=1)

            info_vars = {}
            for label, key in [('ご案内文', 'notice'), ('電話番号等', 'phone')]:
                ttk.Label(left, text=label).pack(anchor='w')
                var = tk.StringVar(value=str(work.get(key, '')))
                ttk.Entry(left, textvariable=var, width=38).pack(fill='x', pady=(0, 10))
                info_vars[key] = var
            ttk.Separator(left).pack(fill='x', pady=7)
            ttk.Label(left, text='背景ロゴ', style='Section.TLabel').pack(anchor='w')
            color_var = tk.StringVar(value=work['logo_color'])
            color_bar = ttk.Frame(left)
            color_bar.pack(fill='x', pady=6)
            ttk.Label(color_bar, text='基本色', width=12).pack(side='left')
            swatch = tk.Label(color_bar, text='  ■  ', font=('Yu Gothic UI', 14, 'bold'),
                              relief='solid', bd=1, bg='#FFFFFF', cursor='hand2')
            swatch.pack(side='left', padx=(1, 7))
            def update_swatch(*_):
                swatch.configure(fg=color_var.get())
            color_var.trace_add('write', update_swatch)
            update_swatch()
            ttk.Label(color_bar, textvariable=color_var, style='Muted.TLabel').pack(side='left')
            def choose_color():
                chosen = colorchooser.askcolor(color=color_var.get(), parent=top,
                                               title='背景ロゴの色')[1]
                if chosen:
                    color_var.set(chosen.upper())
            ttk.Button(color_bar, text='カラーを選択…', style='Action.TButton',
                       command=choose_color).pack(side='right', padx=(5, 0))
            swatch.bind('<Button-1>', lambda e: choose_color())

            scales = {}
            for label, key in [('彩度', 'logo_saturation'), ('明度', 'logo_brightness'),
                               ('背景ロゴ不透明度', 'logo_opacity')]:
                row = ttk.Frame(left)
                row.pack(fill='x', pady=3)
                # ラベルとスライダーを上下に並べ、細い画面でも数値を切らさない。
                ttk.Label(row, text=label).pack(anchor='w', pady=(2, 0))
                var = tk.IntVar(value=int(work.get(key, DEFAULT_SETTINGS[key])))
                scales[key] = var
                ModernSlider(row, var, width=245).pack(fill='x', pady=(0, 2))

            def reset_background():
                color_var.set(DEFAULT_SETTINGS['logo_color'])
                for key in ('logo_saturation', 'logo_brightness', 'logo_opacity'):
                    scales[key].set(DEFAULT_SETTINGS[key])
            ttk.Button(left, text='背景ロゴの色設定を初期設定に戻す',
                       command=reset_background, style='Soft.TButton').pack(fill='x', pady=(5, 10))

            ttk.Separator(left).pack(fill='x', pady=(3, 8))
            ttk.Label(left, text='休業日のピンク濃度',
                      style='Section.TLabel').pack(anchor='w')
            row = ttk.Frame(left)
            row.pack(fill='x', pady=3)
            ttk.Label(row, text='不透明度').pack(anchor='w', pady=(2, 0))
            scales['closure_opacity'] = tk.IntVar(value=int(work.get('closure_opacity', 38)))
            ModernSlider(row, scales['closure_opacity'], width=245).pack(
                fill='x', pady=(0, 2))
            ttk.Button(left, text='ピンク濃度を初期値に戻す',
                       command=lambda: scales['closure_opacity'].set(
                           DEFAULT_SETTINGS['closure_opacity']),
                       style='Soft.TButton').pack(fill='x', pady=(2, 10))

            ttk.Separator(left).pack(fill='x', pady=(3, 8))
            guides = tk.BooleanVar(value=bool(work.get('cut_guides', True)))
            # Clam may render checkboxes with an X, which is easily read as NO.
            # Explicit ON/OFF radio choices avoid any ambiguous cross icon.
            ttk.Label(left, text='裁断マーク（各3mm）',
                      style='Section.TLabel').pack(anchor='w')
            guide_choices = ttk.Frame(left)
            guide_choices.pack(fill='x', pady=(2, 3))
            guide_buttons = []
            for value, label in ((True, '表示する（ON）'), (False, '表示しない（OFF）')):
                btn = tk.Button(guide_choices, text=label, cursor='hand2',
                                font=('Yu Gothic UI', 10, 'bold'),
                                bd=2, relief='raised', padx=12, pady=7,
                                command=lambda v=value: guides.set(v))
                btn.pack(side='left', padx=(0, 9), pady=(1, 3))
                guide_buttons.append((value, btn))
            def refresh_guides(*_):
                for value, button in guide_buttons:
                    active = guides.get() == value
                    button.configure(bg=UI['primary'] if active else '#FFFFFF',
                                     fg='#FFFFFF' if active else UI['ink'],
                                     activebackground='#0F529F' if active else UI['soft'],
                                     activeforeground='#FFFFFF' if active else UI['ink'],
                                     relief='sunken' if active else 'raised',
                                     text=('● ' if active else '○ ') +
                                          ('表示する（ON）' if value else '表示しない（OFF）'))
            guides.trace_add('write', refresh_guides)
            refresh_guides()
            ttk.Separator(left).pack(fill='x', pady=(8, 6))
            ttk.Label(left, text='書体：日付・曜日・見出し（ARP楷書体M優先）',
                      wraplength=330).pack(anchor='w', pady=(7, 0))
            font_var = tk.StringVar(value=str(work.get('calendar_font_file', '')))
            font_status = tk.StringVar(value=self.renderer.kai_font_name)
            ttk.Label(left, textvariable=font_status, foreground='#555',
                      wraplength=330).pack(anchor='w')
            font_buttons = ttk.Frame(left)
            font_buttons.pack(fill='x', pady=(2, 5))
            def select_font():
                filename = filedialog.askopenfilename(parent=top,
                    title='インストール済みフォントを指定',
                    filetypes=[('TrueType / OpenType', '*.ttf *.otf')])
                if filename:
                    font_var.set(filename)
            ttk.Button(font_buttons, text='フォントファイル選択',
                       command=select_font, style='Action.TButton').pack(side='left')
            ttk.Button(font_buttons, text='自動検出に戻す',
                       command=lambda: font_var.set(''), style='Soft.TButton').pack(side='left', padx=(5, 0))
            ttk.Separator(left).pack(fill='x', pady=(9, 7))
            ttk.Button(left, text='祝日データを今すぐ更新',
                       command=lambda: self.update_holidays(silent=False),
                       style='Action.TButton').pack(fill='x', pady=(3, 6))
            ttk.Label(left, text='祝日は起動時に読み込み、24時間ごとに更新確認します。',
                      foreground='#555', wraplength=330).pack(anchor='w')
            # The selected folder itself contains JSON/CSV files (no extra
            # 'data' child is created). A change is committed on Save only.
            ttk.Separator(left).pack(fill='x', pady=(10, 7))
            ttk.Label(left, text='編集データの保存先',
                      style='Section.TLabel').pack(anchor='w')
            data_dir_var = tk.StringVar(value=str(DATA))
            ttk.Label(left, textvariable=data_dir_var, wraplength=325,
                      foreground=UI['muted']).pack(anchor='w', pady=(5, 5))
            def browse_data_dir():
                directory = filedialog.askdirectory(parent=top,
                    title='編集データの保存先フォルダーを選択',
                    initialdir=data_dir_var.get() if Path(data_dir_var.get()).is_dir()
                               else str(Path.home()))
                if directory:
                    data_dir_var.set(str(Path(directory).resolve()))
            ttk.Button(left, text='保存先フォルダーを変更…', style='Action.TButton',
                       command=browse_data_dir).pack(fill='x', pady=(2, 4))
            ttk.Label(left, text='変更時は既存データを新しい保存先へコピーします。元のデータは残ります。\n'
                                 '既存データがあるフォルダーへ切り替える場合は確認します。',
                      wraplength=325, foreground=UI['muted']).pack(anchor='w')

            # ホイール操作を左パネルの子ウィジェットへ登録。
            # bind_allを使わないため、閉じた後に他の画面へ影響しない。
            def scroll_left(event):
                if event.delta:
                    left_canvas.yview_scroll(-int(event.delta / 120) or
                                             (-1 if event.delta > 0 else 1), 'units')
                elif getattr(event, 'num', None) in (4, 5):
                    left_canvas.yview_scroll(-1 if event.num == 4 else 1, 'units')
                return 'break'
            def bind_wheel(widget):
                widget.bind('<MouseWheel>', scroll_left, add='+')
                widget.bind('<Button-4>', scroll_left, add='+')
                widget.bind('<Button-5>', scroll_left, add='+')
                for child in widget.winfo_children():
                    bind_wheel(child)
            bind_wheel(left)
            bind_wheel(left_canvas)

            ttk.Label(right, text='リアルタイムプレビュー（1枚分）',
                      font=('Yu Gothic UI', 11, 'bold')).grid(
                          row=0, column=0, sticky='w')
            ttk.Label(right, text='色を変更するとプレビューが更新されます。',
                      foreground='#555').grid(row=1, column=0, sticky='w')
            preview = tk.Canvas(right, bg=UI['canvas'], highlightthickness=0)
            preview.grid(row=2, column=0, sticky='nsew', pady=(8, 0))
            image_holder = {'image': None, 'job': None}

            def gather():
                for key, var in info_vars.items():
                    work[key] = var.get()
                work['logo_color'] = color_var.get()
                for key, var in scales.items():
                    work[key] = var.get()
                work['cut_guides'] = guides.get()
                work['calendar_font_file'] = font_var.get()

            def redraw_preview():
                image_holder['job'] = None
                if not top.winfo_exists():
                    return
                gather()
                w, h = preview.winfo_width(), preview.winfo_height()
                if w < 25 or h < 25:
                    return
                # 空き領域に画像を収めるため、固定サイズのLabelではなくCanvasを使う。
                scale = min((w-18)/CELL_W, (h-18)/CELL_H, 1.8)
                try:
                    with self.renderer.make_document(self.year, self.month,
                            self.closed.get(self.key(), []), work, single_card=True) as doc:
                        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                               alpha=False)
                    img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                    image_holder['image'] = ImageTk.PhotoImage(img)
                    preview.delete('all')
                    preview.create_image((w-img.width)//2, (h-img.height)//2,
                                         image=image_holder['image'], anchor='nw')
                    font_status.set(self.renderer.kai_font_name)
                except Exception as err:
                    self.status.set(f'設定プレビューを表示できません: {err}')

            def schedule_preview(*_):
                if image_holder['job'] is not None:
                    top.after_cancel(image_holder['job'])
                image_holder['job'] = top.after(120, redraw_preview)
            for var in [color_var, guides, font_var, *scales.values(), *info_vars.values()]:
                var.trace_add('write', schedule_preview)
            preview.bind('<Configure>', schedule_preview)

            def dispose():
                if image_holder['job'] is not None:
                    top.after_cancel(image_holder['job'])
                    image_holder['job'] = None

            def cancel_settings():
                dispose()
                self.renderer._ensure_font(original)
                top.destroy()
            def save_settings():
                gather()
                change_folder = Path(data_dir_var.get()).expanduser().resolve() != DATA.resolve()
                use_existing = False
                saved_settings = dict(work)
                if change_folder:
                    if self.fetching:
                        messagebox.showwarning('保存先の変更',
                            '祝日データを更新中です。更新完了後に保存先を変更してください。', parent=top)
                        return
                    destination = Path(data_dir_var.get()).expanduser().resolve()
                    if ((destination / 'settings.json').exists() or
                            (destination / 'closed_days.json').exists()):
                        use_existing = messagebox.askyesno('既存データの確認',
                            '選択した保存先には、以前のカレンダーデータがあります。\n\n'
                            '「はい」：選択先の既存データに切り替える（今回の設定変更は破棄）\n'
                            '「いいえ」：切り替えずに戻る\n\n'
                            '元のフォルダーは変更・削除しません。', parent=top)
                        if not use_existing:
                            return
                try:
                    if change_folder:
                        switch_data_directory(destination, settings=saved_settings,
                                              use_existing=use_existing)
                        if use_existing:
                            # Switch to the destination's settings/closures as-is.
                            saved_settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
                        self.closed = load_json(CLOSURES_FILE, dict(DEFAULT_RESTS))
                        self.renderer.holidays = read_holidays()
                    else:
                        save_json(SETTINGS_FILE, saved_settings)
                except (OSError, ValueError) as err:
                    messagebox.showerror('設定保存エラー', str(err), parent=top)
                    return
                dispose()
                self.settings = dict(saved_settings)
                top.destroy()
                self.schedule_refresh()
                self.status.set(f'設定を保存しました。データ保存先: {DATA}'
                                if change_folder else '設定を保存しました。')

            ttk.Label(actions, text='設定は「設定を保存」を押すと確定します。',
                      foreground='#555').pack(side='left')
            ttk.Button(actions, text='キャンセル',
                       command=cancel_settings).pack(side='right')
            ttk.Button(actions, text='設定を保存', style='Primary.TButton',
                       command=save_settings).pack(side='right', padx=8)
            top.protocol('WM_DELETE_WINDOW', cancel_settings)
            schedule_preview()

        def run(self):
            self.win.mainloop()

    App().run()


def main():
    parser = argparse.ArgumentParser(description='店舗休業日カレンダー')
    parser.add_argument('--demo', action='store_true', help='画面を開かずPDF出力')
    parser.add_argument('--check', action='store_true', help='実行環境と素材の検証')
    parser.add_argument('--year', type=int, default=2026)
    parser.add_argument('--month', type=int, default=11)
    parser.add_argument('--output', default='sample_output.pdf')
    args = parser.parse_args()
    if args.check:
        for name, path in [('背景ロゴ', KUMO_GEOMETRY),
                           ('店舗ロゴ', NAME_LOGO_PDF),
                           ('内蔵祝日データ', BUILTIN_HOLIDAYS)]:
            if not path.is_file():
                raise FileNotFoundError(f'同梱素材が見つかりません: {name} / {path}')
        print(f'実行形式: {"EXE" if FROZEN else "Python"}')
        print(f'素材: {ASSETS}')
        print(f'編集データ: {DATA}')
        print(f'保存先フォールバック: {DATA_IS_FALLBACK}')
        return
    if args.demo:
        settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        rests = load_json(CLOSURES_FILE, dict(DEFAULT_RESTS))
        year_month = f'{args.year:04d}-{args.month:02d}'
        export_pdf(args.output, args.year, args.month,
                   rests.get(year_month, []), settings)
        print(f'PDF出力: {args.output}')
    else:
        launch_app()


if __name__ == '__main__':
    if FROZEN:
        try:
            main()
        except Exception:
            report = traceback.format_exc()
            try:
                report_path = DATA / 'last_error.txt'
                report_path.write_text(report, encoding='utf-8')
                info = f'\n詳細: {report_path}'
            except OSError:
                info = ''
            try:
                from tkinter import Tk, messagebox
                root = Tk()
                root.withdraw()
                messagebox.showerror('休業日カレンダー作成ツール - エラー',
                    '起動または処理中にエラーが発生しました。' + info + '\n\n' + report[-1200:])
                root.destroy()
            except Exception:
                pass
            raise SystemExit(1)
    else:
        main()
