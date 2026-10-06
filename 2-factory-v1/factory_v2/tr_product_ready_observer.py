"""Shim (FACTORY_SIMPLIFY_S3): moved to products.twitter_radar.tr_product_ready_observer."""
from __future__ import annotations

import sys

from products.twitter_radar import tr_product_ready_observer as _mod

sys.modules[__name__] = _mod

if __name__ == "__main__":
    raise SystemExit(_mod.main())
