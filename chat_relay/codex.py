"""Read and restore one chat in the supported native Codex SQLite layout."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from .common import Busy, Chat, Conflict, SESSION_ID, confined, staged_file, stamp


DATABASES = {'state': 'state_5.sqlite', 'goals': 'goals_1.sqlite', 'history': 'thread_history_1.sqlite'}
TABLES = (
    ('state', 'threads', 'id'),
    ('state', 'thread_dynamic_tools', 'thread_id'),
    ('state', 'thread_attachments', 'thread_id'),
    ('goals', 'thread_goals', 'thread_id'),
    ('goals', 'thread_goal_continuation_deferrals', 'thread_id'),
    ('history', 'thread_turns', 'thread_id'),
    ('history', 'thread_items', 'thread_id'),
    ('history', 'thread_realtime_items', 'thread_id'),
    ('history', 'thread_history_projection_state', 'thread_id'),
)
LOCAL_FIELDS = ('project_id', 'is_pinned', 'thread_section_id', 'section_position', 'section_entered_at_ms')
ROLLOUT = re.compile(r'rollout-(\d{4})-(\d{2})-(\d{2})T[\d-]+-([0-9a-f-]{36})\.jsonl')


def column_name(value):
    return '"' + value.replace('"', '""') + '"'


def columns(db, schema, table):
    return tuple(row[1] for row in db.execute(f'PRAGMA {schema}.table_info({table})'))


def file_prefix(older, newer):
    with older.open('rb') as source, newer.open('rb') as dest:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            if dest.read(len(chunk)) != chunk:
                return False
        return True


def goal_row(db, schema, session):
    return db.execute(f'SELECT * FROM {schema}.thread_goals WHERE thread_id = ?', (session,)).fetchone()


class Codex:
    name = 'codex'

    def __init__(self, home=None, sqlite_home=None):
        self.home = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
        self.sqlite = Path(sqlite_home or (os.environ.get('CODEX_SQLITE_HOME') if home is None else None)
                           or self.home).resolve()

    def available(self):
        return (self.sqlite / DATABASES['state']).is_file()

    def attach(self, db, mode, aliases=DATABASES, *, require=True):
        db.execute('PRAGMA trusted_schema = OFF')
        for alias in aliases:
            path = self.sqlite / DATABASES[alias]
            if path.is_file():
                db.execute(f'ATTACH DATABASE ? AS {alias}', (f'{path.as_uri()}?mode={mode}',))
            elif require:
                raise ValueError(f'Falta {path.name}. Abre y completa una conversación en Codex para inicializar el perfil.')

    def catalog(self):
        if not self.available():
            return {}
        has_goals = (self.sqlite / DATABASES['goals']).is_file()
        with closing(sqlite3.connect(':memory:', uri=True)) as db:
            self.attach(db, 'ro', ('state', 'goals'), require=False)
            join = 'LEFT JOIN goals.thread_goals g ON g.thread_id = t.id' if has_goals else ''
            status = "COALESCE(g.status, '')" if has_goals else "''"
            rows = db.execute(f'''
                SELECT t.id, COALESCE(NULLIF(t.name,''), NULLIF(t.title,''), t.id), t.cwd,
                       {status}, COALESCE(t.updated_at_ms, 1000 * t.updated_at, 0)
                FROM state.threads t {join} WHERE t.has_user_event = 1
            ''').fetchall()
        return {session: Chat(self.name, session, title, cwd, goal, updated)
                for session, title, cwd, goal, updated in rows if SESSION_ID.fullmatch(session)}

    def rollout(self, path, session):
        raw = Path(path)
        if not raw.is_absolute():
            raise ValueError('La ruta del historial de Codex no es absoluta.')
        local = confined(self.home, raw)
        match = ROLLOUT.fullmatch(local.name)
        if not match or match[4] != session or local.relative_to(self.home).parts[0] not in (
                'sessions', 'archived_sessions'):
            raise ValueError('La ruta del historial de Codex es inesperada.')
        return local

    def revision(self, session):
        if not self.available():
            return None
        with closing(sqlite3.connect(':memory:', uri=True)) as db:
            self.attach(db, 'ro', ('state',))
            row = db.execute('SELECT rollout_path, updated_at_ms, updated_at FROM state.threads WHERE id = ?',
                             (session,)).fetchone()
            if row is None:
                return None
            self.attach(db, 'ro', ('goals', 'history'))
            goal = goal_row(db, 'goals', session)
            projection = db.execute('''
                SELECT next_rollout_byte_offset, next_rollout_ordinal
                FROM history.thread_history_projection_state WHERE thread_id = ?
            ''', (session,)).fetchone()
        goal_hash = hashlib.sha256(repr(goal).encode()).hexdigest()
        return [*stamp(self.rollout(row[0], session)), *row[1:], goal_hash, list(projection or ())]

    def export(self, session, bundle, expected):
        with tempfile.TemporaryDirectory() as temp:
            meta = Path(temp) / 'codex.sqlite'
            with closing(sqlite3.connect(meta, uri=True)) as db, db:
                self.attach(db, 'ro')
                db.execute('BEGIN')
                for alias, table, key in TABLES:
                    db.execute(f'CREATE TABLE main.{table} AS SELECT * FROM {alias}.{table} WHERE {key} = ?',
                               (session,))
                row = db.execute('SELECT rollout_path, cwd FROM main.threads').fetchone()
                if not row:
                    raise Busy('El chat de Codex cambió durante la copia.')
            rollout = self.rollout(row[0], session)
            bundle.write(meta, 'codex.sqlite')
            bundle.write(rollout, 'transcript.jsonl')
            bundle.writestr('native.json', json.dumps({'rollout': rollout.name}))
        if self.revision(session) != expected:
            raise Busy('El chat de Codex cambió durante la copia.')
        return row[1]

    def restore(self, session, bundle, workspace, expected, check_idle, *, tracked):
        descriptor = json.loads(bundle.read('native.json'))
        match = ROLLOUT.fullmatch(descriptor['rollout'])
        if not match or match[4] != session:
            raise ValueError('El paquete tiene un historial de Codex inválido.')
        self.home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.home) as temp:
            temp = Path(temp)
            meta, proposed = temp / 'codex.sqlite', temp / 'transcript.jsonl'
            for entry, target in (('codex.sqlite', meta), ('transcript.jsonl', proposed)):
                with bundle.open(entry) as source, target.open('wb') as dest:
                    shutil.copyfileobj(source, dest)
            with closing(sqlite3.connect(':memory:', uri=True)) as db:
                self.attach(db, 'rw')
                db.execute(f'ATTACH DATABASE ? AS incoming', (f'{meta.as_uri()}?mode=ro&immutable=1',))
                allowed = {table for _, table, _ in TABLES}
                if set(db.execute('SELECT name, type FROM incoming.sqlite_schema')) != {
                        (table, 'table') for table in allowed}:
                    raise ValueError('El paquete de Codex contiene tablas inesperadas.')
                for alias, table, _ in TABLES:
                    local_cols = columns(db, alias, table)
                    remote_cols = columns(db, 'incoming', table)
                    if not local_cols or set(local_cols) != set(remote_cols):
                        raise ValueError(f'El formato de {table} difiere. Usa la misma versión de Codex.')
                if db.execute('SELECT id FROM incoming.threads').fetchall() != [(session,)]:
                    raise ValueError('El paquete contiene un identificador de Codex distinto.')
                remote = db.execute('SELECT archived, rollout_path FROM incoming.threads').fetchone()
                if Path(remote[1]).name != match[0]:
                    raise ValueError('El nombre del historial de Codex difiere de su referencia.')
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT rollout_path, cwd FROM state.threads WHERE id = ?', (session,)).fetchone()
                if current is None:
                    folder = self.home / ('archived_sessions' if remote[0] else 'sessions')
                    if not remote[0]:
                        folder = folder.joinpath(*match.groups()[:3])
                    target = confined(self.home, folder / match[0])
                else:
                    target = self.rollout(current[0], session)
                if (current or target.exists()) and (not target.is_file() or not file_prefix(target, proposed)):
                    raise Conflict('El historial local de Codex difiere del recibido.')
                old_goal = goal_row(db, 'goals', session)
                new_goal = goal_row(db, 'incoming', session)
                if old_goal and old_goal != new_goal and (
                        (not new_goal and not tracked) or
                        (new_goal and old_goal[-1] >= new_goal[-1])):
                    raise Conflict('El Goal local de Codex difiere del recibido.')
                if current and target.stat().st_size == proposed.stat().st_size and (
                        old_goal == new_goal and Path(current[1]) == workspace):
                    return
                for alias, table, key in TABLES:
                    self.restore_table(db, alias, table, key, session, target, workspace)
                check_idle()
                if self.revision(session) != expected:
                    raise Busy('Codex cambió la sesión mientras se preparaba la importación.')
                had_file = target.is_file()
                backup = temp / 'previous.jsonl'
                if had_file:
                    os.link(target, backup)
                with staged_file(target) as stage:
                    shutil.copyfile(proposed, stage)
                    check_idle()
                    os.replace(stage, target)
                    try:
                        db.commit()
                    except sqlite3.Error:
                        if had_file:
                            os.replace(backup, target)
                        else:
                            target.unlink()
                        raise

    @staticmethod
    def restore_table(db, alias, table, key, session, target, workspace):
        fields = columns(db, alias, table)
        names = ', '.join(map(column_name, fields))
        if table == 'threads':
            overrides = {'rollout_path': str(target), 'cwd': str(workspace),
                         **dict.fromkeys(LOCAL_FIELDS), 'is_pinned': 0}
            select = ', '.join('?' if field in overrides else column_name(field) for field in fields)
            values = [overrides[field] for field in fields if field in overrides]
            immutable = {'id', *LOCAL_FIELDS}
            update = ', '.join(f'{column_name(f)} = excluded.{column_name(f)}'
                               for f in fields if f not in immutable)
            db.execute(f'''
                INSERT INTO state.threads ({names}) SELECT {select} FROM incoming.threads WHERE id = ?
                ON CONFLICT(id) DO UPDATE SET {update}
            ''', (*values, session))
        else:
            db.execute(f'DELETE FROM {alias}.{table} WHERE {key} = ?', (session,))
            db.execute(f'''
                INSERT INTO {alias}.{table} ({names}) SELECT {names} FROM incoming.{table} WHERE {key} = ?
            ''', (session,))
