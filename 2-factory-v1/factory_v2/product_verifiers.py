"""Shim (FACTORY_SIMPLIFY_S3): moved out of the runtime.
TR_* -> products.twitter_radar.product_verifiers; factory -> factory_v2.factory_verifiers.
Invoked as: python3 -m factory_v2.product_verifiers <MILESTONE_ID> --project-repo .
"""
from __future__ import annotations

import sys

from products.twitter_radar import product_verifiers as _mod
from factory_v2 import factory_verifiers as _factory

for _name in ("verify_external_supervisor", "verify_runrecords_wired",
              "verify_resource_telemetry_true_cost", "verify_audit_loop_cell"):
    setattr(_mod, _name, getattr(_factory, _name))
_mod.FACTORY_MILESTONES = _factory.FACTORY_MILESTONES
_tr_run, _tr_main = _mod.run, _mod.main
def _run(m, r, o, observation_dir=None, **kw):
    kw.setdefault("extra", _factory.FACTORY_MILESTONES)
    return _tr_run(m, r, o, observation_dir, **kw)
_mod.run = _run
_mod.main = lambda argv=None: _tr_main(argv, extra_verifiers=_factory.FACTORY_MILESTONES)
sys.modules[__name__] = _mod

if __name__ == "__main__":
    raise SystemExit(_mod.main())
