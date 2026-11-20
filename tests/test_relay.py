import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from chat_relay.claude import Claude, project_folder
from chat_relay.codex import Codex, DATABASES
from chat_relay.common import Busy, Settings
from chat_relay.relay import Relay, SHARE


CODEX_SCHEMA = {
    'state': '''
        CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT,
          title TEXT, name TEXT, updated_at INTEGER, updated_at_ms INTEGER, has_user_event INTEGER,
          archived INTEGER, project_id TEXT, is_pinned INTEGER, thread_section_id TEXT,
          section_position INTEGER, section_entered_at_ms INTEGER);
        CREATE TABLE thread_dynamic_tools (thread_id TEXT, name TEXT);
        CREATE TABLE thread_attachments (thread_id TEXT, payload TEXT);
    ''',
    'goals': '''
        CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT,
          status TEXT, tokens_used INTEGER, updated_at_ms INTEGER);
        CREATE TABLE thread_goal_continuation_deferrals (thread_id TEXT);
    ''',
    'history': '''
        CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT);
        CREATE TABLE thread_items (thread_id TEXT, item_json TEXT);
        CREATE TABLE thread_realtime_items (thread_id TEXT, item_json TEXT);
        CREATE TABLE thread_history_projection_state (
          thread_id TEXT, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER);
    ''',
}


def append_row(path, **row):
    with path.open('a', encoding='utf-8', newline='\n') as dest:
        dest.write(json.dumps(row) + '\n')


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        self.share, self.a_root, self.b_root = (root / name for name in ('share', 'left', 'right'))
        for path in (self.share, self.a_root / 'demo', self.b_root / 'demo'):
            path.mkdir(parents=True)
        self.ac, self.bc = Claude(root / 'a-claude'), Claude(root / 'b-claude')
        self.ax, self.bx = Codex(root / 'a-codex'), Codex(root / 'b-codex')
        self.a = Relay(root / 'a-relay', providers={'claude': self.ac, 'codex': self.ax}, idle=lambda _: None)
        self.b = Relay(root / 'b-relay', providers={'claude': self.bc, 'codex': self.bx}, idle=lambda _: None)

    def tick(self, relay, root, *keys):
        return relay.tick(Settings(str(self.share), str(root), tuple(keys)))

    def claude_session(self):
        session = str(uuid.uuid4())
        project = self.a_root / 'demo'
        file = self.ac.projects / project_folder(project) / f'{session}.jsonl'
        file.parent.mkdir(parents=True, exist_ok=True)
        append_row(file, type='custom-title', sessionId=session, customTitle='Synthetic fixture')
        append_row(file, type='user', sessionId=session, cwd=str(project), message={'content': 'Fixture prompt'})
        append_row(file, type='attachment', sessionId=session,
                   attachment={'type': 'goal_status', 'met': False, 'condition': 'Fixture condition'})
        return session, f'claude/{session}', file

    def codex_session(self):
        for provider in (self.ax, self.bx):
            provider.home.mkdir()
            for kind, schema in CODEX_SCHEMA.items():
                with sqlite3.connect(provider.home / DATABASES[kind]) as db:
                    db.executescript(schema)
                db.close()
        session = str(uuid.uuid4())
        file = self.ax.home / 'sessions' / '2026' / '01' / '01' / f'rollout-2026-01-01T09-00-00-{session}.jsonl'
        file.parent.mkdir(parents=True)
        append_row(file, type='synthetic-event')
        with sqlite3.connect(self.ax.home / DATABASES['state']) as db:
            db.execute('''
                INSERT INTO threads (id, rollout_path, cwd, title, has_user_event, updated_at, archived, is_pinned)
                VALUES (?, ?, ?, 'Synthetic Codex chat', 1, 1, 0, 1)
            ''', (session, str(file), str(self.a_root / 'demo')))
        db.close()
        with sqlite3.connect(self.ax.home / DATABASES['goals']) as db:
            db.execute('INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?, ?)',
                       (session, 'fixture-goal', 'Synthetic objective', 'paused', 25, 2))
        db.close()
        return session, f'codex/{session}'

    def test_no_selection_or_acceptance_writes_no_transcript(self):
        session, key, _ = self.claude_session()
        ignored, _, _ = self.claude_session()
        self.tick(self.a, self.a_root)
        self.assertFalse((self.share / SHARE).exists())
        self.tick(self.a, self.a_root, key)
        _, offers, _, _ = self.tick(self.b, self.b_root)
        self.assertEqual({key}, offers.keys())
        self.assertIsNone(self.bc.file(session))
        self.assertIsNone(self.bc.file(ignored))
        self.tick(self.a, self.a_root)
        self.assertEqual({}, self.b.remote_offers(self.share / SHARE))

    def test_claude_roundtrip_and_relocated_project(self):
        session, key, _ = self.claude_session()
        self.tick(self.a, self.a_root, key)
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertEqual('Imported', state[key]['status'])
        self.assertEqual(str(self.b_root / 'demo'), self.bc.catalog()[session].cwd)
        self.assertEqual('active', self.bc.catalog()[session].goal)
        append_row(self.bc.file(session), type='attachment', sessionId=session, cwd=str(self.b_root / 'demo'),
                   attachment={'type': 'goal_status', 'met': True})
        self.tick(self.b, self.b_root, key)
        self.tick(self.a, self.a_root, key)
        self.assertEqual('completed', self.ac.catalog()[session].goal)
        self.assertEqual(str(self.a_root / 'demo'), self.ac.catalog()[session].cwd)
        self.tick(self.a, self.a_root, key)
        self.tick(self.b, self.b_root, key)
        self.assertEqual(2, len(list((self.share / SHARE / 'sessions').rglob('*.zip'))))

    def test_conflict_keeps_both_revisions(self):
        session, key, file = self.claude_session()
        self.tick(self.a, self.a_root, key)
        self.tick(self.b, self.b_root, key)
        append_row(file, type='assistant', sessionId=session, message={'content': 'Left branch'})
        append_row(self.bc.file(session), type='assistant', sessionId=session, message={'content': 'Right branch'})
        self.tick(self.a, self.a_root, key)
        _, _, right, _ = self.tick(self.b, self.b_root, key)
        _, _, left, _ = self.tick(self.a, self.a_root, key)
        self.assertIn('Conflict', left[key]['status'])
        self.assertIn('Conflict', right[key]['status'])
        self.assertIn('Left branch', file.read_text(encoding='utf-8'))
        self.assertIn('Right branch', self.bc.file(session).read_text(encoding='utf-8'))
        self.assertEqual(3, len(list((self.share / SHARE / 'sessions').rglob('*.zip'))))

    def test_waits_until_archive_is_complete_and_provider_is_closed(self):
        session, key, _ = self.claude_session()
        self.tick(self.a, self.a_root, key)
        archive, = (self.share / SHARE / 'sessions').rglob('*.zip')
        correct = archive.read_bytes()
        archive.write_bytes(correct[:12])
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertIn('checksum does not match', state[key]['status'])
        archive.write_bytes(correct)

        def busy(_):
            raise Busy('Editor is open')

        self.b.check_idle = busy
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertIn('Editor is open', state[key]['status'])
        self.assertIsNone(self.bc.file(session))
        self.b.check_idle = lambda _: None
        self.tick(self.b, self.b_root, key)
        self.assertTrue(self.bc.file(session).is_file())

    def test_project_path_cannot_escape_local_root(self):
        _, key, _ = self.claude_session()
        self.tick(self.a, self.a_root, key)
        revision, = (self.share / SHARE / 'sessions').rglob('*.json')
        descriptor = json.loads(revision.read_text(encoding='utf-8'))
        descriptor['project'] = '../somewhere'
        revision.write_text(json.dumps(descriptor), encoding='utf-8')
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertIn('Invalid project path', state[key]['status'])

    def test_codex_preserves_native_goal_and_propagates_clear(self):
        session, key = self.codex_session()
        self.tick(self.a, self.a_root, key)
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertEqual('Imported', state[key]['status'])
        chat = self.bx.catalog()[session]
        self.assertEqual('paused', chat.goal)
        self.assertEqual(str(self.b_root / 'demo'), chat.cwd)
        with sqlite3.connect(self.bx.home / DATABASES['goals']) as db:
            self.assertEqual(('Synthetic objective', 25), db.execute(
                'SELECT objective, tokens_used FROM thread_goals').fetchone())
            db.execute('DELETE FROM thread_goals')
        db.close()
        self.tick(self.b, self.b_root, key)
        self.tick(self.a, self.a_root, key)
        self.assertEqual('', self.ax.catalog()[session].goal)

    def test_catalog_includes_codex_chats_with_local_transcripts_regardless_of_event_flag(self):
        session, _ = self.codex_session()
        with sqlite3.connect(self.ax.home / DATABASES['state']) as db:
            db.execute('UPDATE threads SET has_user_event = 0 WHERE id = ?', (session,))
            fake = str(uuid.uuid4())
            db.execute('''
                INSERT INTO threads (id, rollout_path, cwd, title, updated_at, has_user_event)
                VALUES (?, ?, ?, 'Indexed conversation with no local transcript', 1, 1)
            ''', (fake, str(self.ax.home / 'sessions' / f'rollout-2026-01-01T09-00-00-{fake}.jsonl'),
                  str(self.a_root / 'demo')))
        db.close()
        self.assertEqual({session}, self.ax.catalog().keys())

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path')
    def test_codex_accepts_extended_length_workspace_paths(self):
        session, key = self.codex_session()
        with sqlite3.connect(self.ax.home / DATABASES['state']) as db:
            cwd = str(self.a_root / 'demo')
            db.execute('UPDATE threads SET cwd = ? WHERE id = ?', ('\\\\?\\' + cwd, session))
        db.close()
        _, _, state, _ = self.tick(self.a, self.a_root, key)
        self.assertEqual('Backup published', state[key]['status'])

    def test_codex_chat_without_goal(self):
        session, key = self.codex_session()
        with sqlite3.connect(self.ax.home / DATABASES['goals']) as db:
            db.execute('DELETE FROM thread_goals')
        db.close()
        self.tick(self.a, self.a_root, key)
        _, _, state, _ = self.tick(self.b, self.b_root, key)
        self.assertEqual('Imported', state[key]['status'])
        self.assertEqual('', self.bx.catalog()[session].goal)


if __name__ == '__main__':
    unittest.main()
