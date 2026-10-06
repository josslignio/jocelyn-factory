"""Shim: moved to products.twitter_radar.tr_kol_records (restored 2026-09-14 — deleted with the V1 sweep but still the only producer of research/tr_kol_records.json)."""
from __future__ import annotations
import sys
from products.twitter_radar import tr_kol_records as _mod
sys.modules[__name__] = _mod
if __name__ == "__main__":
    raise SystemExit(_mod.main())
