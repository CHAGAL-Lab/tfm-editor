"""Teamfight Manager save editor GUI (tkinter, standard library only). Run it with run_gui.py in the project folder.

Change the star (grade) count, age and traits of the athletes picked in the athlete list. Reading and writing saves is done by core.py.
Changes can only be made while the game is closed, and the save is copied into the `backups` folder first.

Sizes (row height, column width, window) are computed from the font size. Fixed pixel values would clip text on HiDPI screens.

Text comes from i18n.t() (Korean / English). The language picker at the top right rebuilds the whole window, so every label is created
by build() in the current language and nothing keeps a translated string as a value to compare against (see SCOPE_MINE and the star filter).
"""
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import core as se, game, i18n, trait_edit, traits
from .gui_game import SCOPE_ALL, SCOPE_MINE, GameTab
from .i18n import t

POLL_MS = 3000

MAX_TRAIT_SLOTS = 3                    # Number of trait slots shown on the general tab (same as the game screen). The 4th hidden trait is handled on the advanced tab.

COLUMNS = (  # (key, sample text for width, alignment, whether it stretches with the window width); the title is t('col.' + key)
    ('name', '0' * 14, 'w', True),
    ('team', '0' * 16, 'w', True),
    ('age', '40', 'e', False),
    ('stars', '★' * se.MAX_STARS, 'center', False),
    ('salary', '99999', 'e', False),
)
SORT_KEYS = {
    'name': lambda r: r['name'].casefold(),
    'team': lambda r: r['team'].casefold(),
    'age': lambda r: r['age'],
    'stars': lambda r: r['stars'],
    'salary': lambda r: r['salary'],
}


def title():
    return t('app.title')


def stamp(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def stars_text(n):
    return '★' * n if n else '-'


class App:
    def __init__(self, root, first_path=None):
        self.root = root
        self.sv = None
        self.rows = []
        self.by_id = {}
        self.shown_count = 0
        self.game_is_running = False
        self.game_info = {'max_stars': game.FALLBACK_STARS}     # filled in when the advanced tab reads the game
        self.sort = None                # (column key, descending) or None (the save's default order)
        self.save_paths = []
        self.scope = SCOPE_MINE         # the team choice: SCOPE_MINE, SCOPE_ALL or a team name
        self.scope_keys = [SCOPE_MINE, SCOPE_ALL]

        # Get the screen scale from how many pixels an inch is (96 dpi = 1.0). Tk scales fonts by itself, but pixel values must be multiplied by hand.
        self.scale = root.winfo_fpixels('1i') / 96
        self.font = tkfont.nametofont('TkDefaultFont')
        self.bold_font = self.font.copy()
        self.bold_font.configure(weight='bold')
        self.trait_defs = {}
        self.trait_win = None
        self.heading_font = tkfont.nametofont('TkHeadingFont')
        style = ttk.Style(root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('Treeview', rowheight=self.font.metrics('linespace') + self.px(8))
        style.configure('Warn.TLabel', foreground='#b3261e')
        style.configure('Ok.TLabel', foreground='#2e7d32')

        self.save_var = tk.StringVar()
        self.scope_var = tk.StringVar()
        self.query_var = tk.StringVar()
        self.star_filter_var = tk.StringVar()
        self.stars_var = tk.StringVar()
        self.age_var = tk.StringVar()
        self.gold_var = tk.StringVar()
        self.gold_input_var = tk.StringVar()
        self.game_var = tk.StringVar()
        self.count_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.lang_var = tk.StringVar()
        self.query_var.trace_add('write', lambda *a: self.populate())

        self.start_ui()
        self.refresh_save_list(first_path)
        self.poll_game()

    def start_ui(self):
        """Build every widget in the current language. Also what the language picker runs again after a switch."""
        self.root.title(title() + (" v%s" % se.version() if se.version() else ""))
        self.stars_var.set(t('gui.keep'))
        self.age_var.set(t('gui.keep'))
        self.star_filter_var.set(t('gui.any_stars'))
        self.status_var.set(t('gui.loading'))
        self.lang_var.set(i18n.language())
        self.build()
        self.set_team_choices(sorted({r['team'] for r in self.rows}, key=str.casefold))
        self.advanced.refresh()
        self.fit_window()

    def px(self, n):
        return int(n * self.scale)

    def fit_window(self):
        """Keep the window from being smaller than the size the widgets ask for (but not bigger than the screen).
                Notes wrap after the window width is set and the required height grows, so recompute until the size settles."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.update()
        w, h = self.root.winfo_width(), self.root.winfo_height()       # never shrink below the current size
        for _ in range(5):
            self.root.update()
            need_w = min(self.root.winfo_reqwidth(), sw - self.px(40))
            need_h = min(self.root.winfo_reqheight(), sh - self.px(80))
            self.root.minsize(need_w, need_h)
            new = (min(max(self.px(820), need_w, w), sw - self.px(40)), min(max(self.px(600), need_h, h), sh - self.px(80)))
            if new == (w, h):
                break
            w, h = new
            self.root.geometry("%dx%d" % new)

    def on_game_info(self, info):
        """Called every time the advanced tab reads the game. An athlete's stars can only be chosen up to the count the game supports."""
        self.game_info = info
        self.stars_box['values'] = [t('gui.keep')] + [str(i) for i in range(info['max_stars'] + 1)]
        if self.stars_var.get() not in self.stars_box['values']:
            self.stars_var.set(t('gui.keep'))

    def column_width(self, title_text, sample):
        head = self.heading_font.measure(title_text + " ▼") + self.px(24)     # so the sort mark (up/down triangle) does not get clipped
        return max(head, self.font.measure(sample) + self.px(24))

    def heading_text(self, key):
        """A column title, with the sort mark if the list is sorted by it."""
        mark = (" ▼" if self.sort[1] else " ▲") if self.sort and self.sort[0] == key else ""
        return t('col.' + key) + mark

    # ---- Screen ----
    def build(self):
        pad = {'padx': 10, 'pady': 4}

        # Place the bottom row first so that when the window gets narrow the table shrinks and these rows stay.
        self.status_label = ttk.Label(self.root, textvariable=self.status_var, relief='sunken', anchor='w',
                                      padding=(8, 3), justify='left')
        self.status_label.pack(fill='x', side='bottom')
        # long text (file names, etc.) wraps instead of being clipped
        self.status_label.bind('<Configure>', lambda e: self.status_label.configure(wraplength=max(100, e.width - 24)))

        # The language picker: just the two codes, so it reads the same in either language.
        langbar = ttk.Frame(self.root)
        langbar.pack(fill='x', side='top', padx=10, pady=(4, 0))
        self.lang_box = ttk.Combobox(langbar, textvariable=self.lang_var, state='readonly', width=4, values=list(i18n.LANGUAGES))
        self.lang_box.pack(side='right')
        self.lang_box.bind('<<ComboboxSelected>>', lambda e: self.change_language(self.lang_var.get()))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True)
        general = ttk.Frame(self.notebook)
        self.notebook.add(general, text=t('gui.tab_general'))

        gold = ttk.Frame(general)
        gold.pack(fill='x', side='bottom', **pad)
        ttk.Label(gold, text=t('gui.gold')).pack(side='left')
        ttk.Label(gold, textvariable=self.gold_var, width=14).pack(side='left', padx=(6, 6))
        entry = ttk.Entry(gold, textvariable=self.gold_input_var, width=12)
        entry.pack(side='left', padx=(0, 14))
        entry.bind('<Return>', lambda e: self.apply_gold())
        self.gold_btn = ttk.Button(gold, text=t('gui.change_gold'), command=self.apply_gold)
        self.gold_btn.pack(side='left')

        edit = ttk.Frame(general)
        edit.pack(fill='x', side='bottom', **pad)
        ttk.Label(edit, text=t('gui.new_stars')).pack(side='left')
        self.stars_box = ttk.Combobox(edit, textvariable=self.stars_var, state='readonly', width=6,
                                      values=[t('gui.keep')] + [str(i) for i in range(self.game_info['max_stars'] + 1)])
        self.stars_box.pack(side='left', padx=(6, 14))
        ttk.Label(edit, text=t('gui.new_age')).pack(side='left')
        ttk.Combobox(edit, textvariable=self.age_var, width=6,
                     values=[t('gui.keep')] + [str(i) for i in range(se.MIN_AGE, se.MAX_AGE + 1)]).pack(side='left', padx=(6, 14))
        self.apply_btn = ttk.Button(edit, command=self.apply)
        self.apply_btn.pack(side='left')

        act = ttk.Frame(general)
        act.pack(fill='x', side='bottom', **pad)
        ttk.Label(act, textvariable=self.count_var).pack(side='left')
        ttk.Button(act, text=t('gui.restore_open'), command=self.open_restore).pack(side='right')

        top = ttk.Frame(general)
        top.pack(fill='x', **pad)
        ttk.Label(top, text=t('gui.save')).pack(side='left')
        self.save_box = ttk.Combobox(top, textvariable=self.save_var, state='readonly')
        self.save_box.pack(side='left', fill='x', expand=True, padx=8)
        self.save_box.bind('<<ComboboxSelected>>', lambda e: self.load(self.save_paths[self.save_box.current()]))
        ttk.Button(top, text=t('gui.open_file'), command=self.open_file).pack(side='left')
        ttk.Button(top, text=t('gui.refresh'), command=self.reload).pack(side='left', padx=(6, 0))

        self.game_label = ttk.Label(general, textvariable=self.game_var)
        self.game_label.pack(anchor='w', padx=10)

        flt = ttk.Frame(general)
        flt.pack(fill='x', **pad)
        ttk.Label(flt, text=t('col.team')).pack(side='left')
        self.team_box = ttk.Combobox(flt, textvariable=self.scope_var, state='readonly', width=22)
        self.team_box.pack(side='left', padx=(6, 14))
        self.team_box.bind('<<ComboboxSelected>>', lambda e: self.pick_scope())
        ttk.Label(flt, text=t('col.name')).pack(side='left')
        ttk.Entry(flt, textvariable=self.query_var, width=18).pack(side='left', padx=(6, 14))
        ttk.Label(flt, text=t('col.stars')).pack(side='left')
        star_box = ttk.Combobox(flt, textvariable=self.star_filter_var, state='readonly', width=5,
                                values=[t('gui.any_stars')] + [str(i) for i in range(se.MAX_STARS + 1)])
        star_box.pack(side='left', padx=6)
        star_box.bind('<<ComboboxSelected>>', lambda e: self.populate())

        mid = ttk.Frame(general)
        mid.pack(fill='both', expand=True, **pad)
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in COLUMNS], show='headings', selectmode='extended',
                                 height=6)
        for key, sample, anchor, stretch in COLUMNS:
            self.tree.heading(key, text=self.heading_text(key), command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=self.column_width(t('col.' + key), sample), anchor=anchor, stretch=stretch)
        self.tree.tag_configure('mine', background='#e8f1fb')
        bar = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        bar.pack(side='left', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.update_buttons())
        self.tree.bind('<Control-a>', self.select_all)

        # Picking one athlete shows their traits with name and description (read-only).
        self.trait_box = ttk.LabelFrame(general, text=t('gui.traits'))
        self.trait_box.pack(fill='x', before=mid, **pad)
        self.trait_cards = []
        for i in range(MAX_TRAIT_SLOTS):
            # Fix the card height so the window size does not change with the description length (1 name line + 4 description lines).
            card = ttk.Frame(self.trait_box, padding=4, height=self.font.metrics('linespace') * 5 + self.px(16))
            card.pack_propagate(False)
            card.grid(row=0, column=i, sticky='nsew', padx=4, pady=2)
            self.trait_box.columnconfigure(i, weight=1, uniform='trait')
            name = ttk.Label(card, font=self.bold_font)
            desc = ttk.Label(card, justify='left')
            card.bind('<Configure>', lambda e, d=desc: d.configure(wraplength=max(80, e.width - 12)))
            name.pack(anchor='w')
            desc.pack(anchor='w')
            for w in (card, name, desc):
                w.bind('<Button-1>', lambda e, i=i: self.pick_trait(i))
            self.trait_cards.append((name, desc))
        self.show_traits()
        self.root.bind('<F5>', lambda e: self.reload())

        self.advanced = GameTab(self.notebook, self)
        self.notebook.add(self.advanced, text=t('gui.tab_advanced'))
        # Wrap widths of text on a hidden tab are not set yet, so opening the tab can raise the required size.
        self.notebook.bind('<<NotebookTabChanged>>', lambda e: self.root.after_idle(self.fit_window))

    # ---- Language ----
    def change_language(self, code):
        """Switch the language and build the window again. The state worth keeping (open save, filters, sort, selection, tab) is carried over."""
        if code == i18n.language():
            return
        if self.advanced.busy:          # a game-file task runs in a thread and reports back to widgets this would destroy
            self.lang_var.set(i18n.language())
            messagebox.showinfo(title(), t('gui.language_busy'), parent=self.root)
            return
        old_keep, old_any = t('gui.keep'), t('gui.any_stars')
        kept = {'stars': self.stars_var.get(), 'age': self.age_var.get(), 'star_filter': self.star_filter_var.get(),
                'tab': self.notebook.index('current'), 'selected': self.tree.selection(),
                'path': self.sv.path if self.sv else None, 'advanced': self.advanced.export_state()}
        i18n.set_language(code)
        try:
            game.save_setting('lang', code)
        except OSError:                 # not being able to remember the choice must not stop it from working now
            pass
        for w in self.root.winfo_children():
            w.destroy()
        self.start_ui()
        self.stars_var.set(t('gui.keep') if kept['stars'] == old_keep else kept['stars'])
        self.age_var.set(t('gui.keep') if kept['age'] == old_keep else kept['age'])
        self.star_filter_var.set(t('gui.any_stars') if kept['star_filter'] == old_any else kept['star_filter'])
        self.refresh_save_list(kept['path'])
        self.tree.selection_set([i for i in kept['selected'] if self.tree.exists(i)])
        self.advanced.import_state(kept['advanced'])
        self.notebook.select(kept['tab'])
        self.fit_window()

    # ---- Loading a save ----
    def refresh_save_list(self, first_path=None):
        slots = se.list_saves()
        extra = bool(first_path) and first_path not in slots
        if extra:
            slots.append(first_path)
        self.save_paths = slots[::-1]
        self.save_box['values'] = [self.save_label(p, i == 0 and not extra) for i, p in enumerate(self.save_paths)]
        if not self.save_paths:
            self.status_var.set(t('gui.no_saves', dir=se.SAVE_DIR))
            self.update_buttons()
            return
        self.load(first_path or self.save_paths[0])

    @staticmethod
    def save_label(p, newest):
        try:
            return "%s  (%s)%s" % (p.name, stamp(p.stat().st_mtime), t('gui.newest') if newest else "")
        except OSError:
            return p.name

    def open_file(self):
        f = filedialog.askopenfilename(parent=self.root, title=t('gui.pick_save'), initialdir=str(se.SAVE_DIR),
                                       filetypes=[(t('gui.filetype_save'), "*.tfm"), (t('gui.filetype_all'), "*")])
        if not f:
            return
        p = Path(f)
        if p not in self.save_paths:
            self.save_paths.insert(0, p)
            self.save_box['values'] = [self.save_label(q, False) for q in self.save_paths]
        self.load(p)

    def reload(self):
        if self.sv:
            self.load(self.sv.path)

    def load(self, path, quiet=False):
        try:
            sv = se.Save(path)
            rows = sv.rows()
        except Exception as e:
            messagebox.showerror(title(), t('gui.load_error', path=path, error=e), parent=self.root)
            return False
        self.sv, self.rows = sv, rows
        self.trait_defs = {d['key']: d for d in traits.load_traits()}
        self.by_id = {r['id']: r for r in rows}
        self.gold_var.set(t('gui.gold_now', n="{:,}".format(sv.gold)))
        if path in self.save_paths:
            self.save_box.current(self.save_paths.index(path))
        self.set_team_choices(sorted({r['team'] for r in rows}, key=str.casefold))
        self.populate()
        if getattr(self, 'advanced', None):
            self.advanced.populate_players()
        if not quiet:
            self.status_var.set(t('gui.loaded', name=path.name, n=len(rows)))
        return True

    # ---- List ----
    def scope_label(self, key):
        """The text shown for a team choice: the two fixed choices are translated, a team name is shown as it is."""
        return t('gui.scope_mine') if key == SCOPE_MINE else t('gui.scope_all') if key == SCOPE_ALL else key

    def set_team_choices(self, teams):
        """Fill the team choices (my team, all, then the teams in the save). The current choice stays if it still exists."""
        self.scope_keys = [SCOPE_MINE, SCOPE_ALL] + teams
        self.team_box['values'] = [self.scope_label(k) for k in self.scope_keys]
        if self.scope not in self.scope_keys:
            self.scope = SCOPE_MINE
        self.scope_var.set(self.scope_label(self.scope))

    def pick_scope(self):
        self.scope = self.scope_keys[self.team_box.current()]
        self.populate()

    def visible_rows(self):
        scope = self.scope
        query = self.query_var.get().strip().casefold()
        stars = self.star_filter_var.get()          # a number, or the translated "all" which is not one
        out = []
        for r in self.rows:
            if scope == SCOPE_MINE:
                if not r['mine']:
                    continue
            elif scope != SCOPE_ALL and r['team'] != scope:
                continue
            if query and query not in r['name'].casefold():
                continue
            if stars.isdigit() and r['stars'] != int(stars):
                continue
            out.append(r)
        if self.sort:
            key, desc = self.sort
            out.sort(key=SORT_KEYS[key], reverse=desc)
        return out

    def populate(self):
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        shown = self.visible_rows()
        for r in shown:
            self.tree.insert('', 'end', iid=str(r['id']), tags=('mine',) if r['mine'] else (),
                             values=(r['name'], r['team'], r['age'], stars_text(r['stars']), r['salary']))
        self.tree.selection_set([i for i in keep if self.tree.exists(i)])
        self.shown_count = len(shown)
        self.update_buttons()

    def sort_by(self, key):
        self.sort = (key, not self.sort[1] if self.sort and self.sort[0] == key else False)
        for k, *_ in COLUMNS:
            self.tree.heading(k, text=self.heading_text(k))
        self.populate()

    def select_all(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return 'break'

    def selected(self):
        return [self.by_id[int(i)] for i in self.tree.selection()]

    def show_traits(self):
        """When one athlete is picked, show that athlete's trait slots as cards. Clicking a card changes that slot."""
        sel = self.tree.selection()
        slots = self.sv.traits(int(sel[0])) if len(sel) == 1 and self.sv else []
        for i, (name, desc) in enumerate(self.trait_cards):
            if len(sel) != 1:
                name.configure(text=t('gui.pick_one_player') if i == 0 else "")
                desc.configure(text="")
            elif i < len(slots) and slots[i] != traits.EMPTY:
                d = self.trait_defs.get(slots[i])
                name.configure(text=d['name'] if d else slots[i])
                desc.configure(text=d['desc'] if d else t('gui.no_description'))
            elif i < len(slots):
                name.configure(text=t('gui.empty_slot'))
                desc.configure(text=t('gui.click_to_set'))
            elif i == len(slots):
                name.configure(text=t('gui.add_slot'))
                desc.configure(text=t('gui.click_to_add'))
            else:
                name.configure(text="")
                desc.configure(text="")

    def pick_trait(self, slot, aid=None):
        """When a card is clicked: pick a new trait from the list and, once confirmed, change only that slot."""
        if self.trait_win is not None:             # if the window is already open, bring it to the front instead of opening another
            self.trait_win.lift()
            self.trait_win.focus_force()
            return
        if aid is None:
            sel = self.tree.selection()
            if len(sel) != 1 or not self.sv:
                return
            aid = int(sel[0])
        elif not self.sv or aid not in self.sv.aths:
            return
        slots = self.sv.traits(aid)
        if slot > len(slots):
            return
        adding = slot == len(slots)                 # clicking the slot after the last one adds a slot
        if not self.trait_defs:
            messagebox.showinfo(title(), t('gui.no_trait_list'), parent=self.root)
            return
        if self.game_is_running:
            messagebox.showinfo(title(), t('gui.game_running_block'), parent=self.root)
            return
        have = {k for i, k in enumerate(slots) if k != traits.EMPTY and i != slot}     # traits the athlete already has
        key = self.ask_trait(self.by_id[aid]['name'], None if adding else slots[slot], can_empty=not adding, exclude=have)
        if key is None or (not adding and key == slots[slot]):
            return
        allowed = set(self.trait_defs)
        label = lambda k: self.trait_defs[k]['name'] if k in self.trait_defs else t('gui.empty_slot')
        if adding:
            msg = t('gui.confirm_add', player=self.by_id[aid]['name'], trait=label(key),
                    note=self.hidden_note() if slot >= 3 else "")
        else:
            msg = t('gui.confirm_change', player=self.by_id[aid]['name'], old=label(slots[slot]), new=label(key))
        if not messagebox.askyesno(title(), msg, default=messagebox.NO, parent=self.root):
            return
        self.status_var.set(t('gui.changing_traits'))
        self.root.update_idletasks()
        done = self.commit(lambda sv: trait_edit.write_traits(sv, [(aid, slot, key)], allowed))
        if done:
            self.status_var.set(t('gui.traits_done', player=self.by_id[aid]['name'], slot=slot + 1, trait=label(key),
                                  backup=done[0].name, pruned=self.pruned_note()))
            if self.tree.exists(str(aid)):
                self.tree.selection_set(str(aid))
            self.update_buttons()
            if slot >= 3:
                self.advanced.enable_limit()            # for a hidden trait, also turn on the 3-slot display limit

    def hidden_note(self):
        """A note added to the confirmation when setting the 4th (hidden) trait. If the display patch is off, says it will be turned on too."""
        if self.advanced.limit_needed():
            return t('gui.hidden_note')
        return ""

    def ask_trait(self, who, current, can_empty=True, exclude=()):
        """The window for picking a new trait. Return the chosen key, or None if cancelled. 'null' makes the slot empty."""
        order = sorted(self.trait_defs.values(), key=lambda d: d['name'])
        entries = [(traits.EMPTY, t('gui.clear_slot'), "")] * can_empty + [(d['key'], d['name'], d['desc']) for d in order]
        win = tk.Toplevel(self.root)
        win.title(t('gui.pick_trait_title', player=who))
        win.transient(self.root)
        result = []
        suffix = max(len(t('gui.suffix_current')), len(t('gui.suffix_owned')))      # room for "(current)" / "(already has)" after a name
        lst = tk.Listbox(win, exportselection=False, height=min(18, len(entries)),
                         width=max(len(n) for _k, n, _d in entries) + suffix + 1)
        lst.pack(side='left', fill='y', padx=(10, 4), pady=10)
        for i, (k, n, _d) in enumerate(entries):
            lst.insert('end', n + (t('gui.suffix_current') if k == current else t('gui.suffix_owned') if k in exclude else ""))
            if k in exclude:
                lst.itemconfig(i, fg='#9a9a9a')                # gray: the athlete already has this trait
        side = ttk.Frame(win)
        side.pack(side='left', fill='both', expand=True, padx=(4, 10), pady=10)
        box = ttk.Frame(side, width=self.px(300), height=self.font.metrics('linespace') * 8)
        box.pack_propagate(False)                  # fix the size so the window does not change with the description length
        box.pack(anchor='nw', fill='x')
        detail = ttk.Label(box, justify='left', wraplength=self.px(290))
        detail.pack(anchor='nw', fill='x')
        ok = ttk.Button(side, text=t('gui.choose'), state='disabled')
        ok.pack(side='bottom', anchor='e')

        def changed(_e=None):
            i = lst.curselection()
            if i:
                k, n, d = entries[i[0]]
                detail.configure(text="%s\n\n%s" % (n, t('gui.already_owned') if k in exclude else d))
                ok.configure(state='disabled' if k in exclude else 'normal')

        def choose(_e=None):
            i = lst.curselection()
            if i and entries[i[0]][0] not in exclude:
                result.append(entries[i[0]][0])
                win.destroy()
        lst.bind('<<ListboxSelect>>', changed)
        lst.bind('<Double-Button-1>', choose)
        ok.configure(command=choose)
        win.bind('<Escape>', lambda e: win.destroy())
        for i, (k, _n, _d) in enumerate(entries):
            if k == current:
                lst.selection_set(i)
                lst.see(i)
                changed()
        self.trait_win = win
        try:
            win.wait_visibility()
            win.grab_set()
        except tk.TclError:
            pass
        try:
            self.root.wait_window(win)
        finally:
            self.trait_win = None
        return result[0] if result else None

    def update_buttons(self):
        self.show_traits()
        n = len(self.tree.selection())
        self.count_var.set(t('gui.count', shown=self.shown_count, selected=n))
        can_write = bool(self.sv) and not self.game_is_running
        self.apply_btn.configure(text=t('gui.apply', n=n), state='normal' if n and can_write else 'disabled')
        self.gold_btn.configure(state='normal' if can_write else 'disabled')

    # ---- Whether the game is running ----
    def poll_game(self):
        self.game_is_running = se.game_running()
        if self.game_is_running:
            self.game_var.set(t('gui.game_running'))
            self.game_label.configure(style='Warn.TLabel')
        else:
            self.game_var.set(t('gui.game_closed'))
            self.game_label.configure(style='Ok.TLabel')
        self.update_buttons()
        self.advanced.update_buttons()
        self.root.after(POLL_MS, self.poll_game)

    # ---- Apply ----
    @staticmethod
    def parse_int(var, label):
        s = var.get().strip()
        if s in ('', t('gui.keep')):
            return None
        try:
            return int(s)
        except ValueError:
            raise se.EditError(t('gui.not_a_number', label=label, value=s))

    @staticmethod
    def change_text(r, stars, age):
        parts = []
        if stars is not None:
            parts.append(t('gui.change_stars', old=r['stars'], new=stars))
        if age is not None:
            parts.append(t('gui.change_age', old=r['age'], new=age))
        return ", ".join(parts)

    def apply(self):
        targets = self.selected()
        if not targets or not self.sv:
            return
        try:
            stars = self.parse_int(self.stars_var, t('col.stars'))
            age = self.parse_int(self.age_var, t('col.age'))
            se.check_values(stars, age)
            if stars is not None and stars > self.game_info['max_stars']:
                raise se.EditError(t('gui.stars_limit', n=self.game_info['max_stars']))
        except se.EditError as e:
            messagebox.showerror(title(), str(e), parent=self.root)
            return
        if se.count_changes(self.sv, targets, stars, age) == 0:
            messagebox.showinfo(title(), t('gui.no_effect'), parent=self.root)
            return
        lines = ["  %s (%s): %s" % (r['name'], r['team'], self.change_text(r, stars, age)) for r in targets[:10]]
        if len(targets) > 10:
            lines.append(t('gui.more_players', n=len(targets) - 10))
        msg = t('gui.confirm_apply', n=len(targets), lines="\n".join(lines))
        if stars is not None and stars > 3:
            msg += "\n\n" + t('gui.stars_patch_note')
        if not messagebox.askyesno(title(), msg, default=messagebox.NO, parent=self.root):
            return
        done = self.commit(lambda sv: se.write_fields(sv, targets, stars, age))
        if not done:
            return
        backup, changed = done
        self.status_var.set(t('gui.applied', changed=changed, same=len(targets) - changed, backup=backup.name,
                              pruned=self.pruned_note()))
        if stars is not None and stars > 3:
            messagebox.showinfo(title(), se.stars_note(), parent=self.root)

    def apply_gold(self):
        if not self.sv:
            return
        try:
            s = self.gold_input_var.get().strip().replace(',', '')
            if not s:
                raise se.EditError(t('gui.enter_gold'))
            if not s.isdigit():
                raise se.EditError(t('gui.gold_digits', value=s))
            gold = int(s)
            se.check_gold(gold)
        except se.EditError as e:
            messagebox.showerror(title(), str(e), parent=self.root)
            return
        if not se.gold_changes(self.sv, gold):
            messagebox.showinfo(title(), t('gui.gold_same'), parent=self.root)
            return
        msg = t('gui.confirm_gold', old="{:,}".format(self.sv.gold), new="{:,}".format(gold))
        if not messagebox.askyesno(title(), msg, default=messagebox.NO, parent=self.root):
            return
        done = self.commit(lambda sv: se.write_gold(sv, gold))
        if not done:
            return
        backup, changed = done
        self.status_var.set(t('gui.gold_done', result="{:,}".format(gold) if changed else t('gui.gold_unchanged'),
                              backup=backup.name, pruned=self.pruned_note()))

    @staticmethod
    def pruned_note():
        """Text to add to the status bar when old backups were pruned automatically (only the oldest one and the newest 5 are kept)."""
        names = se.take_pruned()
        return t('gui.pruned', n=len(names)) if names else ""

    def commit(self, write):
        """Check that the game is closed and the save is unchanged since it was loaded, back it up, then run write(save).
                On success return (backup file, write's return value) and reload the list. On failure show an error dialog and return None."""
        try:
            se.ensure_game_closed()
            if self.sv.path.read_bytes() != self.sv.data:
                self.load(self.sv.path, quiet=True)
                raise se.EditError(t('gui.save_changed'))
            backup = se.make_backup(self.sv.path)
            result = write(self.sv)
        except se.EditError as e:
            messagebox.showerror(title(), str(e), parent=self.root)
            return None
        except Exception as e:
            messagebox.showerror(title(), t('gui.verify_failed', error="%s: %s" % (type(e).__name__, e)), parent=self.root)
            return None
        self.load(self.sv.path, quiet=True)
        return backup, result

    # ---- Backup restore ----
    def open_restore(self):
        if not self.sv:
            return
        path = self.sv.path
        backups = se.list_backups(path)[::-1]
        win = tk.Toplevel(self.root)
        win.title(t('gui.restore_title'))
        win.geometry("%dx%d+%d+%d" % (self.px(560), self.px(320), self.root.winfo_rootx() + self.px(60),
                                      self.root.winfo_rooty() + self.px(60)))
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=t('gui.restore_header', name=path.name)).pack(anchor='w', padx=10, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill='both', expand=True, padx=10)
        lst = tk.Listbox(box, activestyle='none', exportselection=False)
        sb = ttk.Scrollbar(box, orient='vertical', command=lst.yview)
        lst.configure(yscrollcommand=sb.set)
        lst.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        for b in backups:
            lst.insert('end', "%s    %s" % (stamp(b.stat().st_mtime), b.name))
        if not backups:
            lst.insert('end', t('gui.no_backups'))

        def restore():
            sel = lst.curselection()
            if not backups or not sel:
                return
            src = backups[sel[0]]
            if not messagebox.askyesno(t('gui.restore_title'), t('gui.restore_confirm', name=src.name),
                                       default=messagebox.NO, parent=win):
                return
            try:
                safety = se.restore_backup(path, src)
            except (se.EditError, OSError) as e:
                messagebox.showerror(t('gui.restore_title'), str(e), parent=win)
                return
            win.destroy()
            self.load(path, quiet=True)
            self.status_var.set(t('gui.restore_done', name=src.name, before=safety.name, pruned=self.pruned_note()))

        row = ttk.Frame(win)
        row.pack(fill='x', padx=10, pady=10)
        ttk.Button(row, text=t('gui.close'), command=win.destroy).pack(side='right')
        restore_btn = ttk.Button(row, text=t('gui.restore_selected'), command=restore, state='disabled')
        restore_btn.pack(side='right', padx=(0, 8))
        lst.bind('<<ListboxSelect>>', lambda e: restore_btn.configure(state='normal' if backups else 'disabled'))


def selftest():
    """A check that the executable was bundled correctly (run in CI after the build). Return 1 if there is a problem, 0 if not."""
    problems = []
    if not se.NAMES_FILE.is_file():
        problems.append(t('selftest.no_names', path=se.NAMES_FILE))
    if not se.version():
        problems.append(t('selftest.no_version'))
    try:
        game.pack_tables([[0] * game.COLUMNS] * 2, [[0] * game.COLUMNS] * 2)
        tk.Tk().destroy()
    except Exception as e:                                # a missing display or missing tkinter is also caught here
        problems.append("%s: %s" % (type(e).__name__, e))
    print("selftest:", t('selftest.ok', version=se.version(), dir=se.BACKUP_DIR) if not problems else "; ".join(problems))
    return 1 if problems else 0


def main():
    i18n.init(game.load_settings().get('lang'))
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(se.version() or t('cli.version_unknown'))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    first = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if first and not first.is_file():
        sys.exit(t('cli.error', message=t('cli.no_save_file', path=first)))
    if sys.platform == "win32":
        try:                                              # turn on DPI awareness on Windows so text is not scaled up blurry
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root, first)
    root.mainloop()


if __name__ == '__main__':
    main()
