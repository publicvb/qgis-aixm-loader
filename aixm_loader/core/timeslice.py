"""Reduce a feature's timeslices to its state at one instant (AIXM temporality).

BASELINE / SNAPSHOT slices give the full state; the valid one with the
highest (sequence, correction) wins. PERMDELTA slices are applied on top of
the chosen baseline only when newer than it; TEMPDELTA slices valid at the
instant override properties temporarily.
"""
from datetime import datetime, timezone

from .model import FeatureState


def parse_time(value):
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _valid_at(begin, end, at):
    if at is None:
        return True
    b, e = parse_time(begin), parse_time(end)
    return (b is None or b <= at) and (e is None or at < e)


def _order(ts):
    return (ts.sequence or 0, ts.correction or 0)


def feature_state(feature, at=None):
    """FeatureState valid at ``at`` (aware datetime), or latest when None.

    Returns None when the feature does not exist at that instant.
    """
    slices = feature.timeslices
    base = [t for t in slices if (t.interpretation or "BASELINE") in ("BASELINE", "SNAPSHOT")
            and _valid_at(t.valid_begin, t.valid_end, at)]
    perm = sorted((t for t in slices if t.interpretation == "PERMDELTA"
                   and _valid_at(t.valid_begin, None, at)), key=_order)
    temp = sorted((t for t in slices if t.interpretation == "TEMPDELTA"
                   and _valid_at(t.valid_begin, t.valid_end, at)), key=_order)
    if not base and not perm:
        return None
    if base:
        chosen = max(base, key=_order)
        props = dict(chosen.props)
        perm = [t for t in perm if _order(t) > _order(chosen)]
    else:
        chosen = perm[0]
        props = {}
    for t in perm + temp:
        props.update(t.props)

    lifetime_begin = next((t.lifetime_begin for t in [chosen] + perm if t.lifetime_begin), None)
    lifetime_end = next((t.lifetime_end for t in reversed(perm) if t.lifetime_end), chosen.lifetime_end)
    if at is not None and lifetime_end and parse_time(lifetime_end) and parse_time(lifetime_end) <= at:
        return None
    return FeatureState(
        feature=feature,
        props=props,
        interpretation="TEMPDELTA" if temp else chosen.interpretation,
        valid_begin=chosen.valid_begin,
        valid_end=chosen.valid_end,
        sequence=chosen.sequence,
        correction=chosen.correction,
        lifetime_begin=lifetime_begin,
        lifetime_end=lifetime_end,
    )
