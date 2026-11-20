"""Relay selected sessions via immutable, checksummed revisions in a shared folder."""
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
import zipfile

from .claude import Claude
from .codex import Codex
from .common import (Busy, Conflict, HEX_ID, SHA256, Settings, atomic_json, confined, device_id,
                     file_sha, parse_key, read_json, relative_workspace, staged_file, target_workspace)


LOG = logging.getLogger(__name__)
FAILURES = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile,
            subprocess.SubprocessError)
SHARE = 'ChatRelay-v1'
VERSION = 1
STATUS_VERSION = 2


def check_idle(provider):
    processes = subprocess.run(['tasklist.exe', '/NH', '/FO', 'CSV'], capture_output=True,
                               check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout.lower()
    editors = (b'"code.exe"', b'"code - insiders.exe"')
    names = (b'"codex.exe"',) if provider == 'codex' else (b'"claude.exe"',)
    if any(name in processes for name in (*editors, *names)):
        label = 'Codex' if provider == 'codex' else 'Claude Code'
        raise Busy(f'Close VS Code and {label} to sync this chat.')


class Relay:
    def __init__(self, home, *, providers=None, idle=check_idle):
        self.home = home
        self.device = device_id(home)
        self.providers = providers if providers is not None else {'codex': Codex(), 'claude': Claude()}
        self.check_idle = idle

    def local_chats(self):
        catalog, errors = {}, []
        for provider in self.providers.values():
            try:
                catalog.update((chat.key, chat) for chat in provider.catalog().values())
            except FAILURES as error:
                errors.append(f'{provider.name}: {error}')
        return catalog, errors

    def folder(self, settings):
        return Path(settings.folder).resolve() / SHARE

    @staticmethod
    def chat_dir(root, key):
        provider, session = parse_key(key)
        return confined(root, root / 'sessions' / provider / session)

    def remote_offers(self, root):
        offers = {}
        for path in confined(root, root / 'devices').glob('*.json'):
            if path.stem == self.device or not HEX_ID.fullmatch(path.stem):
                continue
            try:
                data = read_json(path)
                if data['version'] != VERSION or data['device'] != path.stem:
                    continue
                for key, info in data['chats'].items():
                    parse_key(key)
                    if not HEX_ID.fullmatch(info['head']):
                        continue
                    if not all(isinstance(info.get(field, ''), str) for field in ('title', 'goal', 'project')):
                        continue
                    offers.setdefault(key, {})[info['head']] = info
            except FAILURES:
                # MEGA may expose an incomplete manifest before the file has settled.
                continue
        return offers

    def descriptor(self, root, key, head):
        if not HEX_ID.fullmatch(head):
            raise ValueError('Invalid revision ID.')
        data = read_json(confined(root, self.chat_dir(root, key) / f'{head}.json'))
        if data is None:
            raise Busy('Waiting for the revision to arrive in the shared folder.')
        parent = data['parent']
        if (data['version'] != VERSION or data['head'] != head or data['key'] != key or
                not SHA256.fullmatch(data['sha256']) or (parent and not HEX_ID.fullmatch(parent))):
            raise ValueError('The incoming revision has invalid metadata.')
        return data

    def lineage(self, root, key, head):
        seen = set()
        while head:
            if head in seen:
                raise ValueError('The revision history contains a cycle.')
            seen.add(head)
            head = self.descriptor(root, key, head)['parent']
        return seen

    def publish(self, root, key, session, provider, workspace, parent, stamp):
        head = uuid.uuid4().hex
        folder = self.chat_dir(root, key)
        target = folder / f'{head}.zip'
        with staged_file(target) as stage:
            with zipfile.ZipFile(stage, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as bundle:
                cwd = provider.export(session, bundle, stamp)
            relative = relative_workspace(workspace, cwd)
            checksum = file_sha(stage)
            os.replace(stage, target)
        atomic_json(folder / f'{head}.json',
                    dict(version=VERSION, key=key, head=head, parent=parent,
                         project=relative, sha256=checksum))
        return head

    def apply(self, root, key, session, provider, workspace, head, stamp, *, tracked):
        desc = self.descriptor(root, key, head)
        project = target_workspace(workspace, desc['project'])
        if not project.is_dir():
            raise Busy(f'Waiting for the local project folder: {project}')
        archive = confined(root, self.chat_dir(root, key) / f'{head}.zip')
        if not archive.is_file():
            raise Busy('Waiting for the revision archive to arrive.')
        self.check_idle(provider.name)
        with archive.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != desc['sha256']:
                raise Busy('The archive is incomplete or its checksum does not match.')
            source.seek(0)
            with zipfile.ZipFile(source) as bundle:
                provider.restore(session, bundle, project, stamp, lambda: self.check_idle(provider.name),
                                 tracked=tracked)

    def set_status(self, record, key, message):
        if record.get('status') != message:
            LOG.info('%s · %s', key, message)
            record['status'] = message

    def sync_chat(self, root, workspace, key, record, local, remote, save):
        provider_name, session = parse_key(key)
        provider = self.providers[provider_name]
        stamp = provider.revision(session)
        previous, base = record.get('stamp'), record.get('head', '')
        if base and not (self.chat_dir(root, key) / f'{base}.json').is_file():
            raise Busy('The base revision is unavailable in the shared folder.')
        effective_remote = {head for head in remote if head != base}
        lineages = {head: self.lineage(root, key, head) for head in effective_remote}
        ours = self.lineage(root, key, base) if base and effective_remote else {base}
        effective_remote -= ours
        tips = {head for head in effective_remote if not any(
            head != other and head in lineages[other] for other in effective_remote)}
        candidate = next(iter(tips)) if len(tips) == 1 else None
        diverged = len(tips) > 1 or (base and candidate and base not in lineages[candidate])
        changed = stamp is not None and stamp != previous

        if candidate and not diverged and (not base or not changed or record.get('importing') == candidate):
            record['importing'] = candidate
            save()
            try:
                self.apply(root, key, session, provider, workspace, candidate, stamp, tracked=bool(base))
            except Conflict:
                record.pop('importing', None)
                diverged = True
            else:
                record.update(head=candidate, stamp=provider.revision(session), synced=int(time.time()))
                record.pop('importing', None)
                self.set_status(record, key, 'Imported')
                return remote[candidate]

        if changed:
            if base:
                self.check_idle(provider.name)
            record.pop('importing', None)
            head = self.publish(root, key, session, provider, workspace, base, stamp)
            record.update(head=head, stamp=stamp, synced=int(time.time()))
            self.set_status(record, key, 'Conflict · both copies kept' if tips else 'Backup published')
            base = head
        elif diverged or tips:
            self.set_status(record, key, 'Conflict · both copies kept')
        elif not base:
            self.set_status(record, key, 'Waiting for a local or shared chat')
        elif not record.get('status') or record['status'].startswith(('Pending', 'Error', 'Conflict')):
            self.set_status(record, key, 'Up to date')
        if candidate and diverged and not base:
            return None
        if base:
            info = remote.get(base, {})
            project = self.descriptor(root, key, base)['project']
            return {'head': base, 'project': project, 'title': local.title if local else info.get('title', session),
                    'goal': local.goal if local else info.get('goal', '')}
        return None

    def tick(self, settings=None):
        settings = settings or Settings.load(self.home)
        catalog, errors = self.local_chats()
        if not settings.folder or not settings.workspace:
            return catalog, {}, {}, errors
        folder = Path(settings.folder)
        workspace = Path(settings.workspace).resolve()
        if not folder.is_dir():
            return catalog, {}, {}, errors + ['The shared folder is unavailable.']
        root = self.folder(settings)
        remote = self.remote_offers(root)
        if not workspace.is_dir():
            return catalog, remote, {}, errors + ['The local project root is unavailable.']
        suffix = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]
        state_path = self.home / f'state-{suffix}.json'
        state = read_json(state_path, {})
        previous_state = json.dumps(state, sort_keys=True)

        def save():
            atomic_json(state_path, state)

        own_path = confined(root, root / 'devices' / f'{self.device}.json')
        own = read_json(own_path, {})
        former = own.get('chats', {})
        chats = {}
        for key in settings.selected:
            record = state.setdefault(key, {})
            if record.get('status_version') != STATUS_VERSION:
                record.pop('status', None)
                record['status_version'] = STATUS_VERSION
            if key in former:
                chats[key] = former[key]
            try:
                info = self.sync_chat(root, workspace, key, record, catalog.get(key), remote.get(key, {}), save)
                if info is not None:
                    chats[key] = info
            except Busy as error:
                self.set_status(record, key, f'Pending · {error}')
            except FAILURES as error:
                self.set_status(record, key, f'Error · {error}')
        if previous_state != json.dumps(state, sort_keys=True):
            save()
        offer = dict(version=VERSION, device=self.device, chats=chats)
        if offer != own and (chats or own):
            atomic_json(own_path, offer)
        return catalog, remote, {key: state[key] for key in settings.selected}, errors
