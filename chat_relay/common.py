from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid


PROVIDERS = {'codex': 'Codex', 'claude': 'Claude Code'}
SESSION_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
HEX_ID = re.compile(r'[0-9a-f]{32}')
SHA256 = re.compile(r'[0-9a-f]{64}')


class Conflict(ValueError):
    """Both copies need to be preserved."""


class Busy(ValueError):
    """A provider is running or a session is changing."""


@dataclass(frozen=True)
class Chat:
    provider: str
    id: str
    title: str
    cwd: str
    goal: str
    updated: int

    @property
    def key(self):
        return f'{self.provider}/{self.id}'


def parse_key(key):
    provider, sep, session = key.partition('/')
    if sep != '/' or provider not in PROVIDERS or not SESSION_ID.fullmatch(session):
        raise ValueError('Identificador de chat inválido.')
    return provider, session


def local_data():
    return Path(os.environ.get('CHAT_RELAY_HOME') or
                Path(os.environ['LOCALAPPDATA']) / 'ChatRelay').resolve()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


@contextmanager
def staged_file(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.relay-', suffix='.tmp')
    os.close(fd)
    stage = Path(tmp)
    try:
        yield stage
    finally:
        stage.unlink(missing_ok=True)


def atomic_json(path, value):
    with staged_file(path) as stage:
        stage.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        os.replace(stage, path)


def file_sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def stamp(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def regular_path(value):
    value = os.fspath(value)
    if os.name == 'nt':
        drive = Path(value).drive
        if drive.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif drive.startswith('\\\\?\\') and drive.endswith(':'):
            value = value[4:]
    return Path(value)


def relative_workspace(root, cwd):
    if not cwd or not Path(cwd).is_absolute():
        raise ValueError('El chat no tiene una carpeta de proyecto local.')
    try:
        return regular_path(cwd).resolve().relative_to(regular_path(root).resolve()).as_posix()
    except ValueError as error:
        raise ValueError('El proyecto está fuera de la raíz local configurada.') from error


def target_workspace(root, relative):
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or '\\' in relative or ':' in relative or '..' in parts.parts:
        raise ValueError('Ruta de proyecto inválida en el paquete.')
    return confined(root, root.joinpath(*parts.parts))


def confined(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('La ruta apunta fuera de la carpeta esperada.')
    return resolved


@dataclass(frozen=True)
class Settings:
    folder: str = ''
    workspace: str = ''
    selected: tuple = ()

    @classmethod
    def load(cls, home):
        value = read_json(home / 'settings.json', {})
        selected = tuple(sorted(set(value.get('selected', []))))
        for key in selected:
            parse_key(key)
        return cls(value.get('folder', ''), value.get('workspace', ''), selected)

    def save(self, home):
        if self.selected and not (self.folder and self.workspace):
            raise ValueError('Elige la carpeta compartida y la raíz local antes de seleccionar chats.')
        atomic_json(home / 'settings.json',
                    dict(folder=self.folder, workspace=self.workspace, selected=self.selected))


def device_id(home):
    path = home / 'device.json'
    current = read_json(path)
    if current is None:
        current = uuid.uuid4().hex
        atomic_json(path, current)
    if not HEX_ID.fullmatch(current):
        raise ValueError('El identificador local del equipo es inválido.')
    return current
