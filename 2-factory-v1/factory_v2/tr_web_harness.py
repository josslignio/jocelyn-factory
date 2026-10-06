"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.tr_web_harness."""
from __future__ import annotations

import sys

from products.twitter_radar import tr_web_harness as _mod

sys.modules[__name__] = _mod
