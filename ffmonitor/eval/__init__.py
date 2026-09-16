"""Backtesting / evaluation of the recommendation logic on historical seasons.

These tools replay the model's decision rules over completed NFL seasons (where
every outcome is already known) to produce honest, quantified accuracy numbers —
without waiting for the current season to finish. Everything here is strictly
leakage-safe: a call graded on week W may only use data from weeks < W.
"""
