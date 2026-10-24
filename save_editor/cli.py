"""Command line for TFM Editor (Teamfight Manager): change athletes' star (grade, Potential) count and age one by one.

    python3 -m save_editor list                         # my team's athletes (name, age, stars, salary)
    python3 -m save_editor list --all                   # all athletes
    python3 -m save_editor list --team teamname          # find by part of the team name
    python3 -m save_editor set Wraith GALA --stars 4    # pick by name and set to 4 stars
    python3 -m save_editor set Wraith --age 20          # change only the age
    python3 -m save_editor set Wraith --stars 5 --age 20   # stars and age at once
    python3 -m save_editor set --mine --stars 3         # everyone on my team
    python3 -m save_editor gold                         # current gold
    python3 -m save_editor gold 99999                   # change gold
    python3 -m save_editor restore                      # list backups / restore with: restore filename

Advanced (game files, affects every save):
    python3 -m save_editor game                         # game folder, supported star count, patch status
    python3 -m save_editor game aging                   # show the growth curve (growth table)
    python3 -m save_editor game aging --grade 3 --values 400,400,600,400,400,200,150,100,50,-1
    python3 -m save_editor game stars 5                 # raise the supported stars to 5
    python3 -m save_editor game restore                 # game file backup list / restore

Messages are in the language chosen in the GUI (or the computer's language); `--lang ko` / `--lang en` overrides it for one run.
Run it from the project folder (where run_gui.py is). Changes can only be made while the game is closed,
and the save is copied into the `backups` folder first. An athlete's stars can only be set up to the count the game supports (see README.md).
"""
import argparse
import sys
import unicodedata
from pathlib import Path

from . import core, game, i18n
from .core import (KEEP_NEWEST, MAX_AGE, MAX_GOLD, MAX_STARS, MIN_AGE, EditError, Save, check_gold, check_values,
                   count_changes, ensure_game_closed, gold_changes, list_backups, list_saves, make_backup, restore_backup,
                   stars_note, write_fields, write_gold)
from .i18n import t


def die(msg):
    sys.exit(t('cli.error', message=msg))


def report_pruned(names):
    """Report old backups that were pruned automatically (only the oldest one and the newest KEEP_NEWEST are kept)."""
    if names:
        print(t('cli.pruned', n=len(names), keep=KEEP_NEWEST, names=", ".join(names)))


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def pad(s, w):
    return s + ' ' * max(0, w - width(s))


def rpad(s, w):
    return ' ' * max(0, w - width(s)) + s


def pick_save(args):
    if args.save:
        p = Path(args.save)
        if not p.is_file():
            die(t('cli.no_save_file', path=p))
        return p
    slots = list_saves()
    if not slots:
        die(t('cli.no_saves', dir=core.SAVE_DIR))
    return slots[-1]


def team_filter(rows, args):
    if args.all:
        return rows
    if args.team:
        needle = args.team.casefold()
        return [r for r in rows if needle in r['team'].casefold()]
    if args.mine:
        return [r for r in rows if r['mine']]
    return None


def print_rows(rows):
    if not rows:
        print(t('cli.no_players'))
        return
    head_name, head_team = t('col.name'), t('col.team')
    wn = max(width(r['name']) for r in rows + [{'name': head_name}])
    wt = max(width(r['team']) for r in rows + [{'team': head_team}])
    print("%s  %s  %s  %s  %s" % (pad(head_name, wn), pad(head_team, wt), rpad(t('col.age'), 4), pad(t('col.stars'), 5), t('col.salary')))
    for r in rows:
        print("%s  %s  %4d  %s  %d" % (pad(r['name'], wn), pad(r['team'], wt), r['age'], pad('★' * r['stars'] or '-', 5), r['salary']))
    print(t('cli.total', n=len(rows)))


def cmd_list(args, sv):
    rows = sv.rows()
    sel = team_filter(rows, args)
    if sel is None:
        sel = [r for r in rows if r['mine']]
    if args.stars is not None:
        sel = [r for r in sel if r['stars'] == args.stars]
    print_rows(sel)


def find_player(pool, key):
    k = key.casefold()
    exact = [r for r in pool if r['name'].casefold() == k]
    hits = exact or [r for r in pool if k in r['name'].casefold()]
    if not hits:
        die(t('cli.player_not_found', name=key))
    if len(hits) > 1:
        print_rows(hits)
        die(t('cli.player_ambiguous', name=key, n=len(hits)))
    return hits[0]


def change_text(r, stars, age):
    parts = []
    if stars is not None:
        parts.append(t('cli.change_stars', old=r['stars'], new=stars))
    if age is not None:
        parts.append(t('cli.change_age', old=r['age'], new=age))
    return ", ".join(parts)


def check_stars_supported(args):
    """Reject values above the star count the game supports (above it the game can stop while reading the growth table)."""
    if args.stars is None:
        return
    info = game.game_info(args.game_dir)
    if args.stars > info['max_stars']:
        why = t('cli.stars_supported' if info['found'] else 'cli.stars_no_game', n=info['max_stars'])
        raise EditError(t('cli.stars_raise_hint', why=why, count=args.stars))


def cmd_set(args, sv):
    check_values(args.stars, args.age)
    check_stars_supported(args)
    rows = sv.rows()
    scoped = team_filter(rows, args)
    if args.names:
        targets = [find_player(scoped if scoped is not None else rows, n) for n in args.names]
    elif scoped is not None:
        targets = scoped
    else:
        die(t('cli.need_target'))
    targets = list({r['id']: r for r in targets}.values())
    if not targets:
        die(t('cli.no_targets'))
    if len(targets) > 30 and not args.all and not args.yes:
        die(t('cli.confirm_many', n=len(targets)))
    for r in targets:
        print("  %s (%s): %s" % (r['name'], r['team'], change_text(r, args.stars, args.age)))
    if args.dry_run:
        print(t('cli.dry_run_save'))
        return
    if count_changes(sv, targets, args.stars, args.age) == 0:
        print(t('cli.nothing_changed'))
        return
    ensure_game_closed()
    backup = make_backup(sv.path)
    print(t('cli.backup'), backup)
    core.take_pruned()
    changed = write_fields(sv, targets, args.stars, args.age)
    print(t('cli.applied_players', changed=changed, same=len(targets) - changed))
    report_pruned(core.take_pruned())
    if args.stars is not None and args.stars > 3:
        print(stars_note())


def cmd_gold(args, sv):
    print(t('cli.gold', n=sv.gold))
    if args.amount is None:
        return
    check_gold(args.amount)
    print("  %d -> %d" % (sv.gold, args.amount))
    if args.dry_run:
        print(t('cli.dry_run_save'))
        return
    if not gold_changes(sv, args.amount):
        print(t('cli.nothing_changed_gold'))
        return
    ensure_game_closed()
    backup = make_backup(sv.path)
    print(t('cli.backup'), backup)
    core.take_pruned()
    write_gold(sv, args.amount)
    print(t('cli.applied'))
    report_pruned(core.take_pruned())


def cmd_game(args):
    action = args.game_cmd or 'info'
    info = game.game_info(args.game_dir)
    if action == 'info' or not info['found']:
        print(t('cli.game_dir'), info['game_dir'] or t('common.not_found'))
        if not info['found']:
            die(info['error'])
        print(t('cli.game_running'), t('common.yes') if info['running'] else t('common.no'))
        print(t('cli.supported_stars', n=info['max_stars']))
        print(t('cli.star_patch', state=game.dll_state_text(info['dll_state'])))
        return
    g = info['growth']
    if action == 'aging':
        return cmd_game_aging(args, info, g)
    if action == 'stars':
        return cmd_game_stars(args, info)
    cmd_game_restore(args)


def print_aging(g):
    print(" " * 8 + "".join("%8s" % t('common.age_header', age=a) for a in game.AGES))
    for i, row in enumerate(g.base):
        print(pad(t('common.grade', n=i), 8) + "".join("%8d" % v for v in row))
    print(t('cli.aging_note'))


def cmd_game_aging(args, info, g):
    if args.grade is None and args.values is None:
        return print_aging(g)
    if args.grade is None or args.values is None:
        raise EditError(t('cli.need_grade_values'))
    if not 0 <= args.grade < len(g.base):
        raise EditError(t('cli.grade_range', max=len(g.base) - 1))
    try:
        values = [int(v) for v in args.values.split(',')]
    except ValueError:
        raise EditError(t('cli.values_format', n=game.COLUMNS))
    base = [list(r) for r in g.base]
    base[args.grade] = values
    game.check_rows(base, len(g.base), "BaseGet")
    print(t('cli.grade_row', grade=args.grade))
    print(t('cli.row_now'), " ".join("%d" % v for v in g.base[args.grade]))
    print(t('cli.row_new'), " ".join("%d" % v for v in values))
    if args.dry_run:
        print(t('cli.dry_run_game'))
        return
    game.take_pruned()
    backup = game.write_growth(args.game_dir, base)
    print(t('cli.aging_applied') if backup else t('cli.nothing_changed_game'), backup or "")
    print(t('cli.all_saves'))
    report_pruned(game.take_pruned())


def cmd_game_stars(args, info):
    cur = info['max_stars']
    print(t('cli.stars_now', n=cur, state=game.dll_state_text(info['dll_state'])))
    if args.dry_run:
        if not 1 <= args.count <= game.MAX_GRADE_STARS:
            raise EditError(t('game.stars_target_range', max=game.MAX_GRADE_STARS))
        if args.count <= cur:
            raise EditError(t('game.stars_already', n=cur))
        size = game.asset_path(info['game_dir']).stat().st_size / 1e6
        print(t('cli.stars_dry_run', count=args.count, first=cur + 1, size="%.0f" % size))
        print(t('cli.dry_run_game'))
        return
    game.take_pruned()
    for b in game.expand_stars(args.game_dir, args.count, progress=print):
        print(t('cli.backup'), b)
    print(t('cli.stars_done', n=args.count))
    report_pruned(game.take_pruned())


def cmd_game_restore(args):
    backups = game.list_game_backups()
    if not args.file:
        if not backups:
            print(t('cli.no_game_backups'))
        for b in backups:
            print("%-52s %8.1fMB" % (b.name, b.stat().st_size / 1e6))
        if backups:
            print(t('cli.game_restore_hint'))
        return
    match = [b for b in backups if b.name == args.file]
    if not match:
        raise EditError(t('cli.no_such_game_backup', name=args.file))
    game.take_pruned()
    keep = game.restore_game_backup(args.game_dir, match[0], progress=print)
    print(t('cli.restored_game', name=match[0].name))
    print(t('cli.kept_before', name=keep.name) if keep else t('cli.kept_growth'))
    report_pruned(game.take_pruned())


def cmd_restore(args, path):
    backups = list_backups(path)
    if not args.file:
        if not backups:
            print(t('cli.no_backups', name=path.name))
        for b in backups:
            print(b.name)
        if backups:
            print(t('cli.restore_hint'))
        return
    src = core.BACKUP_DIR / args.file
    core.take_pruned()
    safety = restore_backup(path, src)
    print(t('cli.restored', src=src.name, dst=path.name))
    print(t('cli.kept_before', name=safety.name))
    report_pruned(core.take_pruned())


def pick_language(argv):
    """Set the language before the parser is built (its help texts are translated): --lang, else the saved choice, else the computer's language."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--lang', choices=sorted(i18n.LANGUAGES))
    known, _rest = pre.parse_known_args(argv)
    i18n.init(known.lang or game.load_settings().get('lang'))


def main():
    pick_language(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version', version="%(prog)s " + (core.version() or t('cli.version_unknown')))
    ap.add_argument('--lang', choices=sorted(i18n.LANGUAGES), help=t('cli.help_lang'))
    ap.add_argument('--save', help=t('cli.help_save'))
    ap.add_argument('--game-dir', help=t('cli.help_game_dir'))
    sub = ap.add_subparsers(dest='cmd', required=True)

    def scope(p):
        p.add_argument('--mine', action='store_true', help=t('cli.help_mine'))
        p.add_argument('--team', help=t('cli.help_team'))
        p.add_argument('--all', action='store_true', help=t('cli.help_all'))

    p = sub.add_parser('list', help=t('cli.help_list'))
    scope(p)
    p.add_argument('--stars', type=int, help=t('cli.help_list_stars'))
    p = sub.add_parser('set', help=t('cli.help_set'))
    p.add_argument('names', nargs='*', help=t('cli.help_names'))
    scope(p)
    p.add_argument('--stars', type=int, help=t('cli.help_set_stars', max=MAX_STARS))
    p.add_argument('--age', type=int, help=t('cli.help_set_age', min=MIN_AGE, max=MAX_AGE))
    p.add_argument('--dry-run', action='store_true', help=t('cli.help_dry_run'))
    p.add_argument('--yes', action='store_true', help=t('cli.help_yes'))
    p = sub.add_parser('gold', help=t('cli.help_gold'))
    p.add_argument('amount', nargs='?', type=int, help=t('cli.help_gold_amount', max=MAX_GOLD))
    p.add_argument('--dry-run', action='store_true', help=t('cli.help_dry_run'))
    p = sub.add_parser('restore', help=t('cli.help_restore'))
    p.add_argument('file', nargs='?', help=t('cli.help_backup_file'))
    p = sub.add_parser('game', help=t('cli.help_game'))
    gsub = p.add_subparsers(dest='game_cmd')
    gsub.add_parser('info', help=t('cli.help_game_info'))
    a = gsub.add_parser('aging', help=t('cli.help_aging'))
    a.add_argument('--grade', type=int, help=t('cli.help_grade'))
    a.add_argument('--values', help=t('cli.help_values', n=game.COLUMNS))
    a.add_argument('--dry-run', action='store_true', help=t('cli.help_dry_run'))
    s = gsub.add_parser('stars', help=t('cli.help_game_stars'))
    s.add_argument('count', type=int, help=t('cli.help_count', max=game.MAX_GRADE_STARS))
    s.add_argument('--dry-run', action='store_true', help=t('cli.help_dry_run'))
    r = gsub.add_parser('restore', help=t('cli.help_game_restore'))
    r.add_argument('file', nargs='?', help=t('cli.help_backup_file'))
    args = ap.parse_args()

    if args.cmd == 'game':
        try:
            return cmd_game(args)
        except (EditError, OSError) as e:      # OSError: out of disk space, permissions, etc.
            die(str(e))
    path = pick_save(args)
    try:
        if args.cmd == 'restore':
            return cmd_restore(args, path)
        print(t('cli.save', path=path))
        sv = Save(path)
        {'list': cmd_list, 'set': cmd_set, 'gold': cmd_gold}[args.cmd](args, sv)
    except EditError as e:
        die(str(e))


if __name__ == '__main__':
    main()
