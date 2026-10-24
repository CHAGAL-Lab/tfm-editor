"""Find the Steam install and library folders (Windows, Linux). Standard library only."""
import os
import re
import sys
from pathlib import Path


def steam_roots():
    """The Steam install folder candidates that actually exist."""
    roots = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except OSError:
            pass
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(var):
                roots.append(Path(os.environ[var]) / "Steam")
        roots.append(Path(r"C:\Program Files (x86)\Steam"))
    else:
        home = Path.home()
        roots += [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam", home / "snap/steam/common/.local/share/Steam"]
    return [r for r in roots if r.is_dir()]


def steam_libraries():
    """The Steam library folders a game may be installed in (without duplicates). Each has steamapps under it."""
    libs = list(steam_roots())
    for root in steam_roots():
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # paths in vdf have doubled backslashes ("D:\\SteamLibrary")
        libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]
    seen, out = set(), []
    for p in libs:
        key = os.path.normcase(os.path.realpath(p))      # count a folder reached through a link as the same folder
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
