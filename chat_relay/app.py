import argparse
from contextlib import contextmanager
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pystray

from .branding import image
from .common import PROVIDERS, Settings, local_data, regular_path, relative_workspace
from .relay import FAILURES, Relay


POLL_SECONDS = 12
GOALS = {'active': 'Activo', 'completed': 'Completado', 'complete': 'Completado',
         'paused': 'Pausado', 'blocked': 'Bloqueado', 'budget_limited': 'Límite alcanzado',
         'failed': 'Fallido'}
LOG = logging.getLogger(__name__)


@contextmanager
def instance_lock(home):
    with (home / 'instance.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
        else:
            yield True


def open_path(path):
    if path and Path(path).exists():
        os.startfile(str(path))


class App:
    def __init__(self, home, show):
        self.home = home
        self.root = tk.Tk()
        self.root.title('Chat Relay · Configuración')
        self.root.geometry('1110x590')
        self.root.minsize(850, 455)
        self.root.withdraw()
        self.root.protocol('WM_DELETE_WINDOW', self.root.withdraw)
        self.icon_image = image()
        self.settings = Settings.load(home)
        self.picks = set(self.settings.selected)
        self.local, self.remote, self.states, self.errors = {}, {}, {}, []
        self.folder = tk.StringVar(value=self.settings.folder)
        self.workspace = tk.StringVar(value=self.settings.workspace)
        self.filter_provider = tk.StringVar(value='Todos')
        self.filter_text = tk.StringVar()
        self.status = tk.StringVar(value='Buscando sesiones…')
        self.actions = queue.SimpleQueue()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self._build()

        menu = pystray.Menu(
            pystray.MenuItem('Configuración', self.dispatch(self.show_settings), default=True),
            pystray.MenuItem('Sincronizar ahora', self.dispatch(self.wake.set)),
            pystray.MenuItem('Abrir carpeta compartida', self.dispatch(self.open_folder)),
            pystray.MenuItem('Abrir registro', self.dispatch(lambda: open_path(self.home / 'relay.log'))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Salir', self.dispatch(self.quit)),
        )
        self.tray = pystray.Icon('ChatRelay', self.icon_image, 'Chat Relay', menu)
        self.tray.run_detached()
        self.worker = threading.Thread(target=self._worker, name='relay', daemon=True)
        self.worker.start()
        self.root.after(180, self._actions)
        if show:
            self.show_settings()

    def dispatch(self, fn):
        return lambda _icon, _item: self.actions.put((fn, ()))

    def _build(self):
        from PIL import ImageTk

        self.window_icon = ImageTk.PhotoImage(self.icon_image)
        self.root.iconphoto(True, self.window_icon)
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        for row, label, var in ((0, 'Carpeta compartida', self.folder), (1, 'Raíz local de proyectos', self.workspace)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=4)
            ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky='ew', pady=4)
            ttk.Button(frame, text='Examinar…', command=lambda v=var: self.browse(v)).grid(
                row=row, column=2, padx=(8, 0))
        bar = ttk.Frame(frame)
        bar.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(14, 6))
        ttk.Label(bar, text='Asistente').pack(side='left')
        providers = ('Todos', *PROVIDERS.values())
        ttk.Combobox(bar, textvariable=self.filter_provider, values=providers, state='readonly', width=16).pack(
            side='left', padx=(6, 16))
        ttk.Label(bar, text='Buscar chat o proyecto').pack(side='left')
        ttk.Entry(bar, textvariable=self.filter_text, width=38).pack(side='left', padx=6)
        self.workspace.trace_add('write', lambda *_: self.refresh())
        self.filter_provider.trace_add('write', lambda *_: self.refresh())
        self.filter_text.trace_add('write', lambda *_: self.refresh())
        ttk.Label(
            frame, text='Marca los chats que quieres sincronizar en este equipo. Para recibir un chat, márcalo '
                        'también aquí cuando aparezca como Compartido.').grid(
                            row=3, column=0, columnspan=3, sticky='w', pady=(0, 7))
        table = ttk.Frame(frame)
        table.grid(row=4, column=0, columnspan=3, sticky='nsew')
        fields = (
            ('pick', 'Sync', 55), ('provider', 'Asistente', 103), ('title', 'Chat', 250),
            ('project', 'Proyecto', 175), ('goal', 'Goal', 85),
            ('state', 'Estado', 240), ('last', 'Última copia', 120),
        )
        self.tree = ttk.Treeview(table, columns=[i for i, _, _ in fields], show='headings', selectmode='browse')
        for key, label, width in fields:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=min(55, width), stretch=key in ('title', 'project', 'state'))
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<Button-1>', self.click)
        self.tree.bind('<space>', self.space)
        self.tree.bind('<Return>', self.space)
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.describe_selection())
        self.details = tk.StringVar()
        ttk.Label(frame, textvariable=self.details, wraplength=790).grid(
            row=5, column=0, columnspan=3, sticky='w', pady=(7, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=650).grid(row=6, column=0, columnspan=2,
                                                                       sticky='w', pady=(9, 0))
        ttk.Button(frame, text='Guardar selección', command=self.save).grid(row=6, column=2, pady=(9, 0))

    def browse(self, variable):
        path = filedialog.askdirectory(parent=self.root, initialdir=variable.get() or str(Path.home()))
        if path:
            variable.set(str(Path(path)))

    def describe_selection(self):
        key = self.tree.focus()
        if key and self.tree.exists(key):
            row = self.tree.item(key, 'values')
            chat = self.local.get(key)
            self.details.set(' · '.join(filter(None, (row[2], chat.cwd if chat else row[3], row[5]))))
        else:
            self.details.set('')

    def click(self, event):
        if self.tree.identify_column(event.x) == '#1':
            item = self.tree.identify_row(event.y)
            if item:
                self.tree.focus(item)
                self.tree.selection_set(item)
                self.toggle(item)
                return 'break'
        return None

    def space(self, _event):
        item = self.tree.focus()
        if item:
            self.toggle(item)
        return 'break'

    def toggle(self, key):
        if key in self.picks:
            self.picks.remove(key)
        else:
            self.picks.add(key)
        self.refresh()

    def refresh(self):
        provider = self.filter_provider.get()
        query = self.filter_text.get().casefold().strip()
        keep = self.tree.focus()
        prior_view = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        rows = []
        for key in self.local.keys() | self.remote.keys() | self.picks:
            name, session = key.split('/')
            label = PROVIDERS[name]
            if provider not in ('Todos', label):
                continue
            local = self.local.get(key)
            offers = self.remote.get(key, {})
            peer = next(iter(offers.values()), {})
            title = local.title if local else peer.get('title', session)
            project = local.cwd if local else peer.get('project', '')
            goal = local.goal if local else peer.get('goal', '')
            state = self.states.get(key, {})
            status = state.get('status', 'Compartido' if offers else '')
            if query and query not in f'{title} {project} {session}'.casefold():
                continue
            time = datetime.fromtimestamp(state['synced']).strftime('%Y-%m-%d %H:%M') if state.get('synced') else ''
            shown = project
            if local and project:
                try:
                    shown = regular_path(project).relative_to(regular_path(self.workspace.get())).as_posix()
                except ValueError:
                    shown = Path(project).name or project
            values = ('☑' if key in self.picks else '☐', label, title, shown, GOALS.get(goal, goal), status, time)
            rows.append((-(local.updated if local else 0), title.casefold(), key, values))
        for _, _, key, values in sorted(rows):
            self.tree.insert('', 'end', iid=key, values=values)
        if keep and self.tree.exists(keep):
            self.tree.focus(keep)
            self.tree.selection_set(keep)
        if prior_view:
            self.tree.yview_moveto(prior_view[0])
        self.describe_selection()
        count = f'{len(self.picks)} chats seleccionados'
        if self.picks != set(self.settings.selected):
            count += ' · Cambios pendientes de guardar'
        self.status.set(' · '.join([count, *self.errors]))

    def save(self):
        try:
            folder, workspace = self.folder.get().strip(), self.workspace.get().strip()
            values = (('carpeta compartida', folder), ('raíz local de proyectos', workspace))
            for label, value in values:
                if value and (not Path(value).is_absolute() or not Path(value).is_dir()):
                    raise ValueError(f'Elige una carpeta existente para {label}.')
            result = Settings(folder, workspace, tuple(sorted(self.picks)))
            for key in self.picks:
                chat = self.local.get(key)
                if chat and key not in self.settings.selected:
                    try:
                        relative_workspace(Path(workspace), chat.cwd)
                    except ValueError as error:
                        raise ValueError(f'{chat.title} · {error}') from error
            result.save(self.home)
        except (OSError, ValueError) as error:
            messagebox.showerror('Chat Relay', str(error), parent=self.root)
            return
        self.settings = result
        self.wake.set()
        self.refresh()

    def show_settings(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def open_folder(self):
        open_path(self.settings.folder)

    def _worker(self):
        try:
            relay = Relay(self.home)
            while not self.stop.is_set():
                self.wake.clear()
                try:
                    data = relay.tick()
                except FAILURES as error:
                    LOG.exception('Error de sincronización')
                    data = {}, {}, {}, [str(error)]
                self.actions.put((self.results, data))
                self.wake.wait(POLL_SECONDS)
        except Exception:
            LOG.exception('El monitor no pudo iniciar.')
            self.actions.put((self.results, ({}, {}, {}, ['No se pudo iniciar el monitor. Revisa el registro.'])))

    def results(self, local, remote, states, errors):
        self.local, self.remote, self.states, self.errors = local, remote, states, errors
        self.refresh()

    def _actions(self):
        path = self.home / 'show-settings'
        if path.exists():
            path.unlink(missing_ok=True)
            self.show_settings()
        while not self.actions.empty():
            action, args = self.actions.get()
            try:
                action(*args)
            except OSError:
                LOG.exception('No se pudo ejecutar la acción.')
        if not self.stop.is_set():
            self.root.after(180, self._actions)

    def quit(self):
        self.stop.set()
        self.wake.set()
        self.tray.stop()
        self.root.quit()

    def run(self):
        self.root.mainloop()
        # Allow an in-progress export or native import to commit before exit.
        self.worker.join()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(prog='Chat Relay')
    parser.add_argument('--settings', action='store_true', help='Open the settings window.')
    parser.add_argument('--write-icon', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.write_icon:
        image().save(args.write_icon, sizes=[(i, i) for i in (16, 20, 24, 32, 48, 64, 128)])
        return
    home = local_data()
    home.mkdir(parents=True, exist_ok=True)
    with instance_lock(home) as first:
        if not first:
            if args.settings:
                (home / 'show-settings').touch()
            return
        log = RotatingFileHandler(home / 'relay.log', maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
        logging.basicConfig(handlers=[log], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
        try:
            App(home, args.settings or not (home / 'settings.json').exists()).run()
        except Exception:
            LOG.exception('Chat Relay no pudo iniciar.')
            raise
