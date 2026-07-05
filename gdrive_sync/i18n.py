"""Localization: gettext setup for the GUI and the daemon.

Source strings are English; Italian and French catalogs live in po/ and are
installed as /usr/share/locale/<lang>/LC_MESSAGES/gdrive-sync.mo.
The language follows the OS locale (LANGUAGE / LC_MESSAGES / LANG).
"""

from __future__ import annotations

import gettext as _gettext
import locale
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DOMAIN = "gdrive-sync"


def _find_locale_dir() -> str | None:
    """Prefer an in-tree locale dir (development), else the system one."""
    dev = Path(__file__).resolve().parent.parent / "locale"
    if dev.is_dir():
        return str(dev)
    for base in ("/usr/share/locale", "/usr/local/share/locale"):
        if Path(base).is_dir():
            return base
    return None


try:
    # Needed so the C locale of GTK and Python agree on the message language.
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    log.warning("cannot set the system locale; falling back to English")

_LOCALE_DIR = _find_locale_dir()
try:
    # Lets GTK-internal strings (file dialogs, etc.) find our domain too.
    locale.bindtextdomain(DOMAIN, _LOCALE_DIR)
    locale.textdomain(DOMAIN)
except (AttributeError, OSError):
    pass

_translation = _gettext.translation(DOMAIN, localedir=_LOCALE_DIR, fallback=True)

_ = _translation.gettext
ngettext = _translation.ngettext
