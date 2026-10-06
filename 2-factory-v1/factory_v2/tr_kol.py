"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.tr_kol."""
from __future__ import annotations

import sys

from products.twitter_radar import tr_kol as _mod

sys.modules[__name__] = _mod
