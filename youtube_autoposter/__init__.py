"""YouTube AutoPoster - lokales Watch-Folder Upload-System fuer YouTube.

V1 bedient ausschliesslich YouTube. Die Architektur ist bewusst so
gehalten, dass spaeter weitere Plattformen (Instagram/Meta, TikTok,
Spotify, ...) als eigene Adapter unter ``youtube_autoposter.platforms``
ergaenzt werden koennen, ohne die Kern-Pipeline umbauen zu muessen.

Harte Sicherheitsregeln (nicht konfigurierbar):

* Jeder Upload geht IMMER als ``private`` zu YouTube.
* Eine ``privacy_status``-Angabe in einer JSON-Datei kann das NIEMALS
  ueberschreiben (sie wird nur als Wunsch protokolliert).
* Oeffentlich wird ein Video ausschliesslich durch einen expliciten,
  bestaetigten Klick auf "VEROEFFENTLICHEN" in der Oberflaeche.
"""

from __future__ import annotations

from .constants import APP_NAME as __app_name__
from .constants import APP_VERSION as __version__

__all__ = ["__version__", "__app_name__"]
