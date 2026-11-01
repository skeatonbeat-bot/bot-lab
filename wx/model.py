"""Probability for every bracket of one event.

Samples the final settlement value thousands of times:

  future day   each ensemble member's daily max + station bias + noise
  today        what's already been observed is a floor; the hours still ahead come from
               the members, shifted by how far the latest real reading is running off the
               ensemble right now (the "off the forecast" signal), plus bias and noise
  past day     the observed max (settled in all but name)

then converts to the market's unit and rounds half up exactly like the rules do.
Noise widens the ensemble because raw ensembles are known to be overconfident.
"""

import hashlib
from datetime import datetime

import numpy as np

from . import forecast as F
from .markets import in_bracket
from .obs import round_half_up, to_unit

N = 6000
RESID_KEEP = 0.6     # share of the current off-forecast gap assumed to last the rest of today
NOISE_TODAY = 0.7    # °C
NOISE_DAY1 = 1.0     # °C, tomorrow; grows 0.4°C per extra day
MIN_P, MAX_P = 0.003, 0.997


def _rng(key):
    return np.random.default_rng(int(hashlib.sha1(key.encode()).hexdigest()[:8], 16))


def _bucket(samples_c, unit, brackets):
    vals = np.floor(to_unit(samples_c, unit) + 0.5).astype(int)
    out = []
    for b in brackets:
        lo = b["lo"] if b["lo"] is not None else -10_000
        hi = b["hi"] if b["hi"] is not None else 10_000
        out.append(float(np.mean((vals >= lo) & (vals <= hi))))
    return out


def predict(ev, fc, obs_day, now_local, bias_c=0.0):
    """ev: parsed event; fc: forecast.parse output (local times); obs_day: obs.day_summary;
    now_local: naive datetime in the station's local time. Returns dict with probs (one per
    bracket, same order), the sampled median/p10/p90 in the market unit, and the nowcast gap."""
    d = ev["date"]
    today = now_local.date().isoformat()
    rng = _rng(ev["slug"] + now_local.strftime("%Y%m%d%H"))
    obs_max = obs_day.get("max_c") if obs_day else None
    resid = None
    mode = "future"

    if d < today:
        mode = "past"
        if obs_max is None:
            return None
        samples = np.full(N, obs_max)
    elif d == today:
        mode = "today"
        h = now_local.hour
        rem = [x for x in F.member_daily_max(fc, d, from_hour=h + 1) if x is not None]
        latest = obs_day.get("latest") if obs_day else None
        if latest:
            t_obs, c_obs = latest
            lh = round_half_up(t_obs.hour + t_obs.minute / 60.0) % 24
            at = F.member_at(fc, d, lh)
            if at:
                resid = c_obs - float(np.mean(at))
        if rem:
            shift = bias_c + (resid * RESID_KEEP if resid is not None else 0.0)
            draw = np.array(rem)[rng.integers(0, len(rem), N)] + shift + rng.normal(0, NOISE_TODAY, N)
            samples = draw if obs_max is None else np.maximum(draw, obs_max)
        elif obs_max is not None:
            samples = np.full(N, obs_max)
        else:
            return None
    else:
        maxes = [x for x in F.member_daily_max(fc, d) if x is not None]
        if not maxes:
            return None
        lead = (datetime.fromisoformat(d).date() - now_local.date()).days
        sigma = NOISE_DAY1 + 0.4 * max(0, lead - 1)
        samples = np.array(maxes)[rng.integers(0, len(maxes), N)] + bias_c + rng.normal(0, sigma, N)

    probs = _bucket(samples, ev["unit"], ev["brackets"])
    if mode != "past":
        probs = [min(MAX_P, max(MIN_P, p)) for p in probs]
    u = to_unit(samples, ev["unit"])
    return {
        "mode": mode, "probs": probs, "median_c": float(np.median(samples)),
        "median": float(np.median(u)), "p10": float(np.percentile(u, 10)), "p90": float(np.percentile(u, 90)),
        "obs_max": None if obs_max is None else round(to_unit(obs_max, ev["unit"]), 1),
        "gap_c": None if resid is None else round(resid, 2),
        "members": len(fc["members"]) if fc else 0,
    }


def settled_bracket(ev, obs_max_c):
    """Which bracket our own METAR reading says wins (to check against Polymarket's result)."""
    if obs_max_c is None:
        return None
    v = round_half_up(to_unit(obs_max_c, ev["unit"]))
    for b in ev["brackets"]:
        if in_bracket(v, b["lo"], b["hi"]):
            return b["label"]
    return None
