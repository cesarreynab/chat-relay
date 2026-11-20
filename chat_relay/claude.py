"""Claude Code sessions live in per-project JSONL files."""
import json
import os
from pathlib import Path
import re

from .common import Busy, Chat, Conflict, SESSION_ID, confined, staged_file, stamp


def project_folder(cwd):
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(cwd))
    if len(slug) > 200:
        raise ValueError('This Claude Code project path is too long for this version.')
    return slug


def records(source):
    for line in source:
        if not line.endswith(b'\n'):
            raise Busy('Claude Code is still writing the transcript.')
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('Invalid Claude Code transcript record.')
        yield row, line


def same_path(a, b):
    return isinstance(a, str) and os.path.normcase(os.path.normpath(a)) == (
        os.path.normcase(os.path.normpath(b)))


def message_text(row):
    content = row.get('message', {}).get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(part.get('text', '') for part in content
                        if isinstance(part, dict) and part.get('type') == 'text')
    return ''


def describe(session, source):
    title = first = cwd = goal = ''
    for row, _ in records(source):
        if row.get('sessionId', session) != session or row.get('isSidechain'):
            continue
        if row.get('cwd') and not cwd:
            cwd = row['cwd']
        kind = row.get('type')
        if kind == 'custom-title' and isinstance(row.get('customTitle'), str):
            title = row['customTitle']
        elif kind == 'user' and not first and not row.get('isMeta'):
            text = message_text(row).strip()
            if text and not text.startswith(('<local-command-', '<command-name>')):
                first = ' '.join(text.split())[:160]
        elif kind == 'attachment':
            entry = row.get('attachment') or {}
            if entry.get('type') == 'goal_status':
                goal = 'completed' if entry.get('met') else 'active'
        elif kind == 'system' and row.get('subtype') == 'local_command':
            cmd = row.get('commandRun') or {}
            if cmd.get('command') == 'goal':
                output = row.get('content', '')
                if '<local-command-stdout>Goal cleared:' in output:
                    goal = ''
                elif '<local-command-stdout>Goal set:' in output:
                    goal = 'active'
                    if not first and isinstance(cmd.get('args'), str):
                        first = '/goal ' + cmd['args'][:154]
    return title or first or session, cwd, goal


class Claude:
    name = 'claude'

    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').resolve()
        self.projects = self.home / 'projects'
        self.cache = {}

    def file(self, session):
        paths = tuple(self.projects.glob(f'*/{session}.jsonl'))
        if len(paths) > 1:
            raise Conflict('This Claude Code session ID exists in more than one project.')
        return confined(self.projects, paths[0]) if paths else None

    def catalog(self):
        result = {}
        used = set()
        for path in self.projects.glob('*/*.jsonl'):
            session = path.stem
            if not SESSION_ID.fullmatch(session):
                continue
            try:
                safe = confined(self.projects, path)
                info = stamp(safe)
                entry = self.cache.get(path)
                if entry is None or entry[0] != info:
                    with safe.open('rb') as source:
                        title, cwd, goal = describe(session, source)
                    entry = info, Chat(self.name, session, title, cwd, goal, info[1] // 1_000_000)
                    self.cache[path] = entry
                used.add(path)
                if entry[1].cwd:
                    result[session] = entry[1]
            except (OSError, ValueError):
                continue
        self.cache = {p: self.cache[p] for p in used}
        return result

    def revision(self, session):
        path = self.file(session)
        return stamp(path) if path else None

    def export(self, session, bundle, expected):
        path = self.file(session)
        if path is None:
            raise Busy('This Claude Code session is no longer available.')
        with path.open('rb') as source:
            _, cwd, _ = describe(session, source)
        if not cwd:
            raise ValueError('The Claude Code transcript has no project folder.')
        bundle.write(path, 'transcript.jsonl')
        if self.revision(session) != expected:
            raise Busy('The Claude Code transcript changed during export.')
        return cwd

    def restore(self, session, bundle, workspace, expected, check_idle, *, tracked):
        native = confined(self.projects, self.projects / project_folder(workspace) / f'{session}.jsonl')
        original = self.file(session)
        if original and original != native:
            raise Conflict('This Claude Code session already exists in a different project folder.')
        with bundle.open('transcript.jsonl') as source:
            _, source_cwd, _ = describe(session, source)
        if not source_cwd:
            raise ValueError('The Claude Code transcript has no project folder.')
        with staged_file(native) as stage, bundle.open('transcript.jsonl') as source:
            source_rows = records(source)
            with stage.open('wb') as output:
                if original:
                    with original.open('rb') as local:
                        for row, line in records(local):
                            incoming = next(source_rows, None)
                            if incoming is None or self.relocate(incoming[0], source_cwd, workspace) != row:
                                raise Conflict('The local Claude Code transcript differs from the incoming copy.')
                            output.write(line)
                for row, line in source_rows:
                    identity = row.get('sessionId', session)
                    if identity != session:
                        raise ValueError('The archive contains records from a different Claude Code session.')
                    relocated = self.relocate(row, source_cwd, workspace)
                    output.write(line if relocated is row else
                                 (json.dumps(relocated, ensure_ascii=False) + '\n').encode('utf-8'))
            if self.revision(session) != expected:
                raise Busy('The Claude Code session changed while preparing the import.')
            if original and original.stat().st_size == stage.stat().st_size:
                return
            check_idle()
            os.replace(stage, native)

    @staticmethod
    def relocate(row, origin, workspace):
        if same_path(row.get('cwd'), origin) and not same_path(row['cwd'], str(workspace)):
            return {**row, 'cwd': str(workspace)}
        return row
