"""Gedeelde locatie-constanten (Utrecht Oost) — één bron voor alle pipelines.

Alleen stdlib. De per-project modules her-binden deze namen aan hun eigen
lokale aliassen (UTRECHT_LAT, _LAT, …) zodat call-sites en tests ongewijzigd
blijven; dit bestand is uitsluitend de bron van de getallen.
"""

from zoneinfo import ZoneInfo

LATITUDE = 52.0907
LONGITUDE = 5.1214
TZ = ZoneInfo("Europe/Amsterdam")
