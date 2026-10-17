"""本机共享文件限制；API 和连接器在每次操作开始时读取。单位 MiB。"""
import json
import os
import tempfile
from pathlib import Path

DEFAULTS = {'download_mb': 1024, 'upload_mb': 1024, 'parse_mb': 256}


def settings_path():
    if os.environ.get('STUDY_FILE_LIMITS_PATH'):
        return Path(os.environ['STUDY_FILE_LIMITS_PATH'])
    base = (Path(os.environ.get('LOCALAPPDATA') or Path.home()) if os.name == 'nt'
            else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'))
    return base / 'zjulearn' / 'controlled-browser' / 'file-limits.json'


def read_settings():
    try:
        data = json.loads(settings_path().read_text(encoding='utf-8'))
        return {key: value for key, value in data.items() if key in DEFAULTS
                and type(value) is int and 1 <= value <= (1024 if key == 'parse_mb' else 4096)}
    except (OSError, ValueError, AttributeError):
        return {}


def limit_bytes(kind, fallback):
    value = read_settings().get(kind + '_mb')
    return value * 1024 * 1024 if value is not None else fallback


def save_settings(values):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(values, stream)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
