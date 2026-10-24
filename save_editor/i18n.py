"""Korean / English text for the GUI, the CLI and error messages. Standard library only.

Every string the user can see lives in lang_ko.py and lang_en.py under the same key. `t(key, **values)` looks the key up in the
current language at the moment it is called (never at import time), so the language can be switched while the program runs.
Values go into `{name}` placeholders (str.format) instead of `%d`, because word order differs between the two languages.
"""
import locale
import os
import sys

from . import lang_en, lang_ko

LANGUAGES = ('ko', 'en')                           # the codes shown as the choices of the language picker
DEFAULT = 'en'
CATALOGS = {'ko': lang_ko.MESSAGES, 'en': lang_en.MESSAGES}
_current = DEFAULT


def detect():
    """The language of the computer's settings: Korean if the system is set to Korean, English otherwise."""
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x12:      # 0x12 = Korean
                return 'ko'
        except (AttributeError, OSError):
            pass
    try:
        names = [locale.getlocale()[0]]
    except ValueError:
        names = []
    names += [os.environ.get(v) for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    for name in names:
        if name and name.lower().startswith(("ko", "korean")):
            return 'ko'
    return DEFAULT


def init(saved=None):
    """Pick the language at startup: the saved choice if there is one, else the computer's language."""
    set_language(saved if saved in LANGUAGES else detect())


def set_language(code):
    global _current
    if code not in LANGUAGES:
        raise ValueError(code)
    _current = code


def language():
    return _current


def t(key, **values):
    """The text for key in the current language. A key missing from the Korean catalog falls back to English."""
    text = CATALOGS[_current].get(key)
    if text is None:
        text = CATALOGS[DEFAULT][key]
    return text.format(**values) if values else text
