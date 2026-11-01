# Runaways and collapses: does the crowd overshoot?

_Generated 2026-11-01 15:41 UTC by research/spikes.py from the spring 2026 Polymarket 5-minute order-book recording._ 'Rushed side' = the side the price just ran toward. Gap = how often it won minus its price right after the move (negative = the crowd overshot). ROI per $1 buying at the ask with the taker fee; |t| under ~2 is noise.

## Runaways (15c+ in 20 seconds): 74,407 windows

**By move size**

| move size | events | price after | rushed side won | gap | next 30s drift | follow ROI (t) | fade ROI (t) |
|---|---|---|---|---|---|---|---|
| 15-25c | 69,231 | 0.65 | 67.0% | +0.021 | +0.007 | -3.3% (-12.1) | -17.7% (-36.3) |
| 25-40c | 4,205 | 0.74 | 74.4% | +0.007 | -0.012 | -5.0% (-5.6) | -18.6% (-7.8) |
| 40c+ | 305 | 0.81 | 83.3% | +0.018 | -0.026 | -2.6% (-1.0) | -27.1% (-2.5) |

**By time in window**

| time in window | events | price after | rushed side won | gap | next 30s drift | follow ROI (t) | fade ROI (t) |
|---|---|---|---|---|---|---|---|
| 0:30-1:30 | 56,549 | 0.66 | 67.3% | +0.014 | +0.002 | -4.1% (-13.9) | -15.8% (-28.8) |
| 1:30-3:00 | 15,155 | 0.65 | 68.0% | +0.034 | +0.017 | -1.7% (-2.9) | -22.2% (-20.7) |
| 3:00-4:00 | 2,703 | 0.64 | 69.9% | +0.060 | +0.033 | +0.6% (+0.4) | -34.4% (-14.0) |

**By rushed side's new price**

| rushed side's new price | events | price after | rushed side won | gap | next 30s drift | follow ROI (t) | fade ROI (t) |
|---|---|---|---|---|---|---|---|
| under 50c | 8,453 | 0.43 | 42.5% | -0.001 | +0.011 | -10.2% (-8.8) | -7.3% (-8.3) |
| 50-70c | 38,761 | 0.62 | 64.0% | +0.020 | +0.009 | -3.5% (-9.5) | -15.2% (-26.1) |
| 70-85c | 23,697 | 0.76 | 78.6% | +0.025 | +0.000 | -1.4% (-4.1) | -22.6% (-22.9) |
| 85c+ | 3,496 | 0.89 | 91.7% | +0.029 | -0.007 | +0.7% (+1.4) | -38.5% (-10.7) |

## Collapses (an 80c+ favorite falls to 50c or below within 30 seconds): 9,153 windows

Here the rushed side is the new leader; fading it = backing the fallen favorite.

| time in window | events | price after | rushed side won | gap | next 30s drift | follow ROI (t) | fade ROI (t) |
|---|---|---|---|---|---|---|---|
| 0:30-2:00 | 1,197 | 0.55 | 54.3% | -0.008 | -0.002 | -10.6% (-4.4) | -10.7% (-3.7) |
| 2:00-3:30 | 3,546 | 0.55 | 57.8% | +0.026 | +0.015 | -4.1% (-2.9) | -16.1% (-9.4) |
| 3:30-4:30 | 4,410 | 0.57 | 60.8% | +0.038 | +0.025 | -2.4% (-2.0) | -19.3% (-12.0) |
