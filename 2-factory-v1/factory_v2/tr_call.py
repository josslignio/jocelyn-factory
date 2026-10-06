"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.tr_call."""
from __future__ import annotations

import sys

from products.twitter_radar import tr_call as _mod

sys.modules[__name__] = _mod
