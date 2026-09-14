"""Machine-learning / advanced-analytics module for the fantasy monitor.

This subpackage is OPTIONAL and self-contained. It depends on
`requirements-ml.txt` (nfl_data_py, pandas) and pulls historical NFL game logs
— a completely separate data source from the daily ESPN/Sleeper snapshots the
core monitor uses.

Build order (see README "Machine learning" section):
  1. data.py     — pull weekly stats, engineer per-player-week usage features
  2. baseline.py — rule-based "rising usage" signal (the baseline to beat)
  3. (future) train.py / predict.py — a breakout classifier that must
     outperform the baseline to earn its place
"""

DEFAULT_POSITIONS = ("RB", "WR", "TE")
