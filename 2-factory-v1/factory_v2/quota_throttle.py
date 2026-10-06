"""Parallelism guard driven by the factory's merged quota view."""
from __future__ import annotations

from collections.abc import Mapping


def _utilisations(value):
    if isinstance(value, Mapping):
        utilisation = value.get("utilisation")
        if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool):
            yield float(utilisation)
        for child in value.values():
            yield from _utilisations(child)


REFUSE_AT = 0.90      # same threshold as quota_observer: an engine at/over it is REFUSED
SLOW_AT = 0.75


def _per_provider_peaks(view: Mapping) -> dict:
    peaks = {}
    for name, windows in (view or {}).items():
        if name.startswith("_") or name == "quota_waste_incident" or not isinstance(windows, Mapping):
            continue
        peaks[str(name)] = max(_utilisations(windows), default=0.0)
    return peaks


def effective_workers(view: dict, configured: int) -> int:
    """Return a positive worker limit no greater than configured.

    ``view`` is the result of ``quota_observer.merged_view`` (provider ->
    windows). 21/09 06:50 Bangkok: the first version took the peak across ALL
    providers, so GLM sitting at 97 % (refused, unused until its reset) throttled
    the whole factory to one worker while Claude was at 0 %. A refused provider
    is simply not usable; the throttle follows the providers that ARE usable:
    the most constrained usable provider decides, and only when no provider is
    usable at all does the pool fall to a single worker. The daemon also
    supplies ``quota_waste_incident=True`` while its rolling three-hour waste
    incident is active — that always means serial execution.
    """
    try:
        ceiling = max(1, int(configured))
    except (TypeError, ValueError):
        ceiling = 1
    if isinstance(view, Mapping) and view.get("quota_waste_incident") is True:
        return 1
    peaks = _per_provider_peaks(view if isinstance(view, Mapping) else {})
    if not peaks:                      # a bare windows mapping (single provider) or nothing
        peak = max(_utilisations(view or {}), default=0.0)
        usable = [peak] if peak < REFUSE_AT else []
    else:
        usable = [pk for pk in peaks.values() if pk < REFUSE_AT]
    if not usable:
        return 1
    peak = max(usable)
    if peak >= SLOW_AT:
        return max(1, (ceiling + 1) // 2)
    return ceiling
