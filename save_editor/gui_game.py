"""The GUI advanced tab. The growth sub-tab handles game files (growth curve, increasing the star count); the traits sub-tab handles the hidden trait and the 3-slot trait display patch.
Features that change game files affect every save, so they are kept apart from the general tab.

Reading and writing game files is done by game.py. The files are big and can take time, so writes run in a thread
and progress messages are shown on screen. Error and confirmation dialogs are shown only from the main thread.

This tab is built again whenever the language is switched (see App.change_language), so it can hand over what the user was
in the middle of with export_state() and take it back with import_state().
"""
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import core as se, game, trait_edit
from .i18n import t

PUMP_MS = 100
SCOPE_MINE, SCOPE_ALL = "\0mine", "\0all"      # keys of the two fixed team choices; any other key is a team name (same as gui.py)


def stamp(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def wrapping_label(parent, text):
    """A label whose long note wraps at the window width."""
    lb = ttk.Label(parent, text=text, justify='left')
    # Set the wrap width from the start. Otherwise a long note asks for a whole line of width and the window gets wider.
    lb.configure(wraplength=int(lb.winfo_fpixels('1i') / 96 * 700))
    lb.bind('<Configure>', lambda e: lb.configure(wraplength=max(100, e.width - 8)))
    return lb


class GameTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title = app.root.title()
        self.info = None
        self.busy = False
        self.queue = queue.Queue()
        self.done = None
        self.cells = []                      # input cells of the growth curve table: cells[row][column] = StringVar
        self.dir_var = tk.StringVar()
        self.stars_info_var = tk.StringVar()
        self.dll_var = tk.StringVar()
        self.run_var = tk.StringVar()
        self.target_var = tk.StringVar()
        self.task_var = tk.StringVar()
        self.hidden_var = tk.StringVar()
        self.decline_var = tk.BooleanVar(value=True)
        self.decline_mask = []                # cells that were decline (negative) in the loaded table; the decline toggle changes these
        self.limit_var = tk.StringVar()
        self.scope = SCOPE_MINE
        self.scope_var = tk.StringVar()
        self.query_var = tk.StringVar()
        self.build()

    # ---- Screen ----
    def build(self):
        pad = {'padx': 10, 'pady': 4}

        box = ttk.LabelFrame(self, text=t('adv.game_box'), padding=8)
        box.pack(fill='x', **pad)
        row = ttk.Frame(box)
        row.pack(fill='x')
        ttk.Label(row, text=t('adv.game_dir')).pack(side='left')
        ttk.Entry(row, textvariable=self.dir_var, state='readonly').pack(side='left', fill='x', expand=True, padx=8)
        self.dir_btn = ttk.Button(row, text=t('adv.choose_dir'), command=self.choose_dir)
        self.dir_btn.pack(side='left')
        self.reload_btn = ttk.Button(row, text=t('gui.refresh'), command=self.refresh)
        self.reload_btn.pack(side='left', padx=(6, 0))
        for var in (self.stars_info_var, self.dll_var):
            ttk.Label(box, textvariable=var).pack(anchor='w')
        self.run_label = ttk.Label(box, textvariable=self.run_var, style='Warn.TLabel')
        self.run_label.pack(anchor='w')

        # Place the bottom row first so that when the window gets narrow the sub-tabs shrink and this row stays.
        row = ttk.Frame(self)
        row.pack(fill='x', side='bottom', **pad)
        self.restore_btn = ttk.Button(row, text=t('adv.restore_open'), command=self.open_restore)
        self.restore_btn.pack(side='left')
        ttk.Label(row, textvariable=self.task_var).pack(side='left', padx=10)

        # Sub-tabs: growth (game file: growth curve, star count), traits (save: hidden trait)
        sub = self.sub = ttk.Notebook(self)
        sub.pack(fill='both', expand=True, padx=10, pady=4)
        sub.bind('<<NotebookTabChanged>>', lambda e: self.app.root.after_idle(self.app.fit_window))
        growth = ttk.Frame(sub)
        sub.add(growth, text=t('adv.tab_growth'))
        trait = ttk.Frame(sub)
        sub.add(trait, text=t('adv.tab_traits'))

        aging = ttk.LabelFrame(growth, text=t('adv.aging_box'), padding=8)
        aging.pack(fill='x', pady=4)
        wrapping_label(aging, t('adv.aging_note')).pack(fill='x', pady=(0, 6))
        self.grid_frame = ttk.Frame(aging)
        self.grid_frame.pack(anchor='w')
        row = ttk.Frame(aging)
        row.pack(fill='x', pady=(8, 0))
        self.aging_btn = ttk.Button(row, text=t('adv.apply_aging'), command=self.apply_aging)
        self.aging_btn.pack(side='left')
        ttk.Button(row, text=t('adv.reload_values'), command=self.fill_grid).pack(side='left', padx=(8, 0))
        ttk.Button(row, text=t('adv.normalize'), command=self.normalize_grid).pack(side='left', padx=(8, 0))
        ttk.Checkbutton(row, text=t('adv.decline'), variable=self.decline_var,
                        command=self.toggle_decline).pack(side='left', padx=(16, 0))

        stars = ttk.LabelFrame(growth, text=t('adv.stars_box'), padding=8)
        stars.pack(fill='x', pady=4)
        wrapping_label(stars, t('adv.stars_note')).pack(fill='x', pady=(0, 6))
        row = ttk.Frame(stars)
        row.pack(fill='x')
        ttk.Label(row, text=t('adv.target')).pack(side='left')
        self.target_box = ttk.Combobox(row, textvariable=self.target_var, state='readonly', width=4)
        self.target_box.pack(side='left', padx=(6, 10))
        self.expand_btn = ttk.Button(row, text=t('adv.expand'), command=self.expand)
        self.expand_btn.pack(side='left')

        view = ttk.LabelFrame(trait, text=t('adv.limit_box'), padding=6)
        view.pack(fill='x', pady=(4, 2))
        row = ttk.Frame(view)
        row.pack(fill='x')
        self.limit_btn = ttk.Button(row, text=t('adv.limit_on'), command=self.toggle_trait_limit)
        self.limit_btn.pack(side='left')
        ttk.Label(row, textvariable=self.limit_var).pack(side='left', padx=10)

        hidden = ttk.LabelFrame(trait, text=t('adv.hidden_box', n=trait_edit.MAX_SLOTS - 3), padding=6)
        hidden.pack(fill='both', expand=True, pady=(2, 4))
        flt = ttk.Frame(hidden)
        flt.pack(fill='x', pady=(0, 4))
        ttk.Label(flt, text=t('adv.scope')).pack(side='left')
        self.scope_box = ttk.Combobox(flt, textvariable=self.scope_var, state='readonly', width=16)
        self.scope_box.pack(side='left', padx=(6, 12))
        self.scope_box.bind('<<ComboboxSelected>>', lambda e: self.pick_scope())
        ttk.Label(flt, text=t('col.name')).pack(side='left')
        entry = ttk.Entry(flt, textvariable=self.query_var, width=14)
        entry.pack(side='left', padx=6)
        self.query_var.trace_add('write', lambda *a: self.populate_players())

        mid = ttk.Frame(hidden)
        mid.pack(fill='both', expand=True)
        self.ptree = ttk.Treeview(mid, columns=('name', 'team', 'traits', 'hidden'), show='headings', selectmode='browse', height=5)
        for key, title, width, stretch in (('name', t('col.name'), 90, False), ('team', t('col.team'), 80, False),
                                           ('traits', t('adv.col_traits'), 290, False), ('hidden', t('adv.col_hidden'), 200, True)):
            self.ptree.heading(key, text=title)
            self.ptree.column(key, width=self.app.px(width), anchor='w', stretch=stretch)
        bar = ttk.Scrollbar(mid, orient='vertical', command=self.ptree.yview)
        self.ptree.configure(yscrollcommand=bar.set)
        self.ptree.pack(side='left', fill='both', expand=True)
        bar.pack(side='left', fill='y')
        self.ptree.bind('<<TreeviewSelect>>', lambda e: self.update_hidden())

        row = ttk.Frame(hidden)
        row.pack(fill='x', pady=(6, 0))
        self.hidden_btns = []
        for i in range(trait_edit.MAX_SLOTS - 3):
            btn = ttk.Button(row, width=16, command=lambda i=i: self.click_hidden(i))
            btn.pack(side='left', padx=(0, 6))
            self.hidden_btns.append(btn)
        ttk.Label(row, textvariable=self.hidden_var).pack(side='left', padx=4)

    # ---- Carrying the state over a language switch ----
    def export_state(self):
        return {'scope': self.scope, 'query': self.query_var.get(), 'sub': self.sub.index('current'),
                'cells': [[v.get() for v in row] for row in self.cells], 'decline': self.decline_var.get(),
                'mask': self.decline_mask, 'target': self.target_var.get(), 'selected': self.ptree.selection()}

    def import_state(self, state):
        self.scope = state['scope']
        self.query_var.set(state['query'])
        if [len(r) for r in self.cells] == [len(r) for r in state['cells']]:       # only if the same table was read again
            for vars_, values in zip(self.cells, state['cells']):
                for var, value in zip(vars_, values):
                    var.set(value)
            self.decline_mask = state['mask']
            self.decline_var.set(state['decline'])
        if state['target'] in self.target_box['values']:
            self.target_var.set(state['target'])
        self.populate_players()
        self.ptree.selection_set([i for i in state['selected'] if self.ptree.exists(i)])
        self.sub.select(state['sub'])

    def fill_grid(self):
        """Fill the input cells with the growth table currently read from the game."""
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.cells = []
        g = self.info.get('growth') if self.info and self.info['found'] else None
        if not g:
            ttk.Label(self.grid_frame, text=t('adv.grid_placeholder')).pack()
            return
        for c, age in enumerate(game.AGES):
            ttk.Label(self.grid_frame, text=t('common.age_header', age=age), anchor='center').grid(row=0, column=c + 1, padx=2)
        for r, row in enumerate(g.base):
            ttk.Label(self.grid_frame, text=t('common.grade', n=r)).grid(row=r + 1, column=0, sticky='e', padx=(0, 6))
            vars_ = []
            for c, v in enumerate(row):
                var = tk.StringVar(value=str(v))
                ttk.Entry(self.grid_frame, textvariable=var, width=7, justify='right').grid(row=r + 1, column=c + 1, padx=2, pady=1)
                vars_.append(var)
            self.cells.append(vars_)
        self.decline_mask = game.decline_mask(g.base)
        self.decline_var.set(any(any(m) for m in self.decline_mask))

    def set_grid(self, base):
        for vars_, row in zip(self.cells, base):
            for var, v in zip(vars_, row):
                var.set(str(v))

    def normalize_grid(self):
        """Tidy the table into one shape where a higher star is the same or better at every age and fill the input cells (apply separately)."""
        if not self.cells:
            return
        try:
            new = game.normalize_curve(self.read_grid())
        except se.EditError as e:
            self.show_error(e)
            return
        self.decline_mask = game.decline_mask(new)
        self.decline_var.set(True)
        self.set_grid(new)

    def toggle_decline(self):
        """Fill the input cells with the decline cells set to -1 (decline) or 0 (no decline). Turning it off can make a veteran's next salary keep rising."""
        if not self.cells:
            return
        try:
            base = self.read_grid()
        except se.EditError as e:
            self.show_error(e)
            return
        on = self.decline_var.get()
        self.set_grid(game.set_decline(base, self.decline_mask, on))
        if not on:
            self.app.status_var.set(t('adv.decline_off_note'))

    def refresh(self):
        """Read the game folder, growth table and patch status again."""
        info = self.info = game.game_info()
        self.app.on_game_info(info)
        self.dir_var.set(str(info['game_dir']) if info['game_dir'] else t('common.not_found'))
        if info['found']:
            self.stars_info_var.set(t('adv.supported_stars', n=info['max_stars']))
            self.dll_var.set(t('adv.star_patch', state=game.dll_state_text(info['dll_state'])))
            self.limit_var.set(t('adv.limit_' + info['trait_limit']))
            self.limit_btn.configure(text=t('adv.limit_off') if info['trait_limit'] == "patched" else t('adv.limit_on'))
        else:
            self.limit_var.set("")
            self.stars_info_var.set(t('adv.not_found', error=info['error']))
            self.dll_var.set("")
        values = ([str(i) for i in range(info['max_stars'] + 1, game.MAX_GRADE_STARS + 1)] if info['found'] else [])
        self.target_box['values'] = values
        self.target_var.set(values[-1] if values else "")
        self.fill_grid()
        self.update_buttons()

    def update_buttons(self):
        found = bool(self.info and self.info['found'])
        ok = found and not self.busy and not self.app.game_is_running
        for btn, cond in ((self.limit_btn, ok and self.info['trait_limit'] != "unknown"), (self.aging_btn, ok), (self.expand_btn, ok and bool(self.target_box['values'])),
                          (self.restore_btn, ok), (self.dir_btn, not self.busy), (self.reload_btn, not self.busy)):
            btn.configure(state='normal' if cond else 'disabled')
        self.run_var.set(t('adv.game_running') if self.app.game_is_running else "")
        self.update_hidden()

    def pick_scope(self):
        self.scope = self.app.scope_keys[self.scope_box.current()]
        self.populate_players()

    def populate_players(self):
        """Fill the traits sub-tab's athlete list filtered by scope and name. Called every time the save is reloaded."""
        app = self.app
        if not getattr(app, 'sv', None):
            return
        self.scope_box['values'] = [app.scope_label(k) for k in app.scope_keys]
        if self.scope not in app.scope_keys:
            self.scope = SCOPE_MINE
        self.scope_var.set(app.scope_label(self.scope))
        scope, query = self.scope, self.query_var.get().strip().casefold()
        keep = self.ptree.selection()
        self.ptree.delete(*self.ptree.get_children())
        texts = {'name': [], 'team': [], 'traits': [], 'hidden': []}
        for r in app.rows:
            keys = app.sv.traits(r['id'])
            names = [app.trait_defs[k]['name'] if k in app.trait_defs else k for k in keys]
            normal = " · ".join(n for n, k in zip(names[:3], keys[:3]) if k != 'null')
            hidden = " · ".join(n if k != 'null' else t('gui.empty_slot') for n, k in zip(names[3:], keys[3:])) or "-"
            values = (r['name'], r['team'], normal or "-", hidden)
            for key, value in zip(texts, values):
                texts[key].append(value)
            if (scope == SCOPE_MINE and not r['mine']) or (scope not in (SCOPE_MINE, SCOPE_ALL) and r['team'] != scope):
                continue
            if query and query not in r['name'].casefold():
                continue
            self.ptree.insert('', 'end', iid=str(r['id']), values=values)
        self.fit_columns(texts)
        self.ptree.selection_set([i for i in keep if self.ptree.exists(i)])
        self.update_hidden()

    def fit_columns(self, texts):
        """Widen the columns to the longest text in them, up to a limit, so names are not cut off (English trait names are longer than Korean ones).
                texts maps a column key to the strings it shows; it covers every player, so the widths do not jump while the filter changes."""
        font = self.app.font
        for key, base, limit in (('name', 90, 200), ('team', 80, 260), ('traits', 290, 480), ('hidden', 200, 400)):
            longest = max((font.measure(s) for s in texts[key]), default=0)
            self.ptree.column(key, width=max(self.app.px(base), min(longest + self.app.px(24), self.app.px(limit))))

    def selected_player(self):
        sel = self.ptree.selection()
        return int(sel[0]) if sel else None

    def click_hidden(self, i):
        """A hidden trait button was clicked: change that slot, or add it if it is the first empty one."""
        aid = self.selected_player()
        if aid is not None:
            self.app.pick_trait(3 + i, aid)

    def update_hidden(self):
        """Show the picked athlete's hidden trait slots on the buttons and enable only the ones that can be used."""
        app, aid = self.app, self.selected_player()
        slots = app.sv.traits(aid) if aid is not None and app.sv else None
        msg = ""
        if slots is None:
            msg = t('adv.pick_player')
        elif len(slots) < 3:
            msg = t('adv.needs_three')
        for i, btn in enumerate(self.hidden_btns):
            n = 3 + i
            if slots is None or len(slots) < 3 or n > len(slots):
                text, ok = "-", False
            elif n == len(slots):
                text, ok = t('adv.add'), True
            else:
                d = app.trait_defs.get(slots[n])
                text, ok = ((d['name'] if d else slots[n]) if slots[n] != 'null' else t('gui.empty_slot')), True
            btn.configure(text=text, state='normal' if ok and not app.game_is_running else 'disabled')
        self.hidden_var.set(msg)

    # ---- Running tasks ----
    def run(self, fn, done):
        """Run fn (taking a progress callback) in a thread and, when it ends, call done(result, error) on the main thread."""
        if self.busy:
            return
        self.busy, self.done = True, done
        self.update_buttons()

        def work():
            try:
                self.queue.put(('done', fn(lambda m: self.queue.put(('progress', m))), None))
            except Exception as e:
                self.queue.put(('done', None, e))

        threading.Thread(target=work, daemon=True).start()
        self.after(PUMP_MS, self.pump)

    def pump(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == 'progress':
                    self.task_var.set(item[1])
                    continue
                self.busy = False
                self.task_var.set("")
                self.update_buttons()
                self.done(item[1], item[2])
                return
        except queue.Empty:
            pass
        self.after(PUMP_MS, self.pump)

    def show_error(self, e):
        if isinstance(e, se.EditError):
            msg = str(e)
        else:
            msg = t('adv.task_failed', error="%s: %s" % (type(e).__name__, e))
        messagebox.showerror(self.title, msg, parent=self.app.root)

    # ---- Game folder ----
    def choose_dir(self):
        d = filedialog.askdirectory(parent=self.app.root, title=t('adv.pick_dir'))
        if not d:
            return
        if not game.is_game_dir(Path(d)):
            messagebox.showerror(self.title, t('adv.not_game_dir'), parent=self.app.root)
            return
        game.save_game_dir(d)
        self.refresh()

    # ---- Growth curve ----
    def read_grid(self):
        base = []
        for r, vars_ in enumerate(self.cells):
            row = []
            for c, var in enumerate(vars_):
                s = var.get().strip()
                try:
                    row.append(int(s))
                except ValueError:
                    raise se.EditError(t('adv.cell_not_int', grade=t('common.grade', n=r), age=game.AGES[c], value=repr(s)))
            base.append(row)
        return base

    def apply_aging(self):
        g = self.info.get('growth')
        if not g:
            return
        try:
            base = self.read_grid()
            game.check_rows(base, len(g.base), "BaseGet")
        except se.EditError as e:
            messagebox.showerror(self.title, str(e), parent=self.app.root)
            return
        changed = [(r, c) for r, row in enumerate(base) for c, v in enumerate(row) if v != g.base[r][c]]
        if not changed:
            messagebox.showinfo(self.title, t('adv.no_change'), parent=self.app.root)
            return
        msg = t('adv.confirm_aging', n=len(changed))
        if not messagebox.askyesno(self.title, msg, default=messagebox.NO, parent=self.app.root):
            return
        game_dir = self.info['game_dir']
        game.take_pruned()
        self.run(lambda progress: game.write_growth(game_dir, base), self.after_aging)

    @staticmethod
    def pruned_note():
        names = game.take_pruned()
        return t('gui.pruned', n=len(names)) if names else ""

    def after_aging(self, backup, err):
        note = self.pruned_note()
        if err:
            self.show_error(err)
        self.refresh()
        if not err:
            self.app.status_var.set((t('adv.aging_done', name=backup.name) if backup else t('adv.no_change')) + note)

    # ---- Increasing the star count ----
    def expand(self):
        if not self.target_var.get():
            return
        target = int(self.target_var.get())
        size = game.asset_path(self.info['game_dir']).stat().st_size / 1e6
        msg = t('adv.confirm_expand', target=target, size="%.0f" % size)
        if not messagebox.askyesno(self.title, msg, default=messagebox.NO, parent=self.app.root):
            return
        game_dir = self.info['game_dir']
        game.take_pruned()
        self.run(lambda progress: game.expand_stars(game_dir, target, progress), self.after_expand)

    def toggle_trait_limit(self):
        on = self.info['trait_limit'] != "patched"
        if on and not messagebox.askyesno(
                self.title, t('adv.confirm_limit'), default=messagebox.NO, parent=self.app.root):
            return
        game_dir = self.info['game_dir']
        game.take_pruned()
        self.run(lambda progress: game.set_trait_limit(game_dir, on), lambda b, err: self.after_limit(on, b, err))

    def limit_needed(self):
        """The hidden trait needs the 3-slot display limit to be on. True if it is not on yet and can be turned on."""
        i = self.info
        return bool(i and i['found'] and i['trait_limit'] in ("original", "partial"))

    def enable_limit(self):
        """Turn the 3-slot display limit on automatically after a hidden trait is set. Tell the reason if it cannot be turned on."""
        i = self.info
        if not (i and i['found']):
            messagebox.showwarning(self.title, t('adv.limit_no_game'), parent=self.app.root)
        elif i['trait_limit'] == "unknown":
            messagebox.showwarning(self.title, t('adv.limit_dll_unknown'), parent=self.app.root)
        elif self.limit_needed():
            game_dir = i['game_dir']
            game.take_pruned()
            self.run(lambda progress: game.set_trait_limit(game_dir, True), lambda b, err: self.after_limit(True, b, err))

    def after_limit(self, on, backup, err):
        note = self.pruned_note()
        if err:
            self.show_error(err)
        self.refresh()
        if not err:
            self.app.status_var.set(t('adv.limit_status', what=t('adv.limit_what_on' if on else 'adv.limit_what_off'),
                                      backup=t('adv.backup_suffix', name=backup.name) if backup else t('adv.already_state'),
                                      pruned=note))

    def after_expand(self, backups, err):
        note = self.pruned_note()
        if err:
            self.show_error(err)
        self.refresh()
        if not err:
            self.app.status_var.set(t('adv.expand_done', n=self.info['max_stars'], backups=", ".join(b.name for b in backups),
                                      pruned=note))

    # ---- Game backup restore ----
    def open_restore(self):
        backups = game.list_game_backups()[::-1]
        win = tk.Toplevel(self.app.root)
        win.title(t('adv.restore_title'))
        win.geometry("%dx%d+%d+%d" % (self.app.px(680), self.app.px(320), self.app.root.winfo_rootx() + self.app.px(60),
                                      self.app.root.winfo_rooty() + self.app.px(60)))
        win.transient(self.app.root)
        win.grab_set()
        ttk.Label(win, text=t('adv.restore_header')).pack(anchor='w', padx=10, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill='both', expand=True, padx=10)
        lst = tk.Listbox(box, activestyle='none', exportselection=False)
        sb = ttk.Scrollbar(box, orient='vertical', command=lst.yview)
        lst.configure(yscrollcommand=sb.set)
        lst.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        for b in backups:
            lst.insert('end', "%s    %s  (%.1fMB)" % (stamp(b.stat().st_mtime), b.name, b.stat().st_size / 1e6))
        if not backups:
            lst.insert('end', t('gui.no_backups'))

        def restore():
            sel = lst.curselection()
            if not backups or not sel:
                return
            src = backups[sel[0]]
            what = t('adv.restore_what_growth') if src.suffix == ".json" else t('adv.restore_what_dll')
            if not messagebox.askyesno(t('adv.restore_title'), t('adv.restore_confirm', name=src.name, what=what),
                                       default=messagebox.NO, parent=win):
                return
            game_dir = self.info['game_dir']
            win.destroy()
            game.take_pruned()
            self.run(lambda progress: game.restore_game_backup(game_dir, src, progress),
                     lambda keep, err: self.after_restore(src, keep, err))

        row = ttk.Frame(win)
        row.pack(fill='x', padx=10, pady=10)
        ttk.Button(row, text=t('gui.close'), command=win.destroy).pack(side='right')
        btn = ttk.Button(row, text=t('adv.restore_selected'), command=restore, state='disabled')
        btn.pack(side='right', padx=(0, 8))
        lst.bind('<<ListboxSelect>>', lambda e: btn.configure(state='normal' if backups else 'disabled'))

    def after_restore(self, src, keep, err):
        note = self.pruned_note()
        if err:
            self.show_error(err)
        self.refresh()
        if not err:
            self.app.status_var.set(t('adv.restore_done', name=src.name,
                                      before=t('adv.restore_before', name=keep.name) if keep else "", pruned=note))
