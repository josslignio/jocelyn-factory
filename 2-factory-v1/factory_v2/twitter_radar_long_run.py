"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.twitter_radar_long_run."""
from __future__ import annotations

import sys

from products.twitter_radar import twitter_radar_long_run as _mod

sys.modules[__name__] = _mod
