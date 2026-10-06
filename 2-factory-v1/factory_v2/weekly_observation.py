"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.weekly_observation."""
from __future__ import annotations

import sys

from products.twitter_radar import weekly_observation as _mod

sys.modules[__name__] = _mod
