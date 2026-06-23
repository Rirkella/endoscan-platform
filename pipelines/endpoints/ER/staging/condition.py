"""Condition selection + MCF7/A549 early fusion (transcriptomics only).

Mapping (FLAGGED for human confirmation; documented in the dataset card):
- **Condition rule:** prefer 10 uM / 24 h. If a compound+cell lacks it, fall back
  to the nearest available dose/time (by |log10 dose - log10 10| then |time - 24|),
  rather than dropping the compound — ER-labelled compounds are scarce, so N is
  preserved. Deterministic tie-break by ``sig_id``.
- **Early fusion:** mean-aggregate each compound's selected MCF7 and A549 landmark
  profiles into ONE 978-vector per compound; ``group = compound`` (InChIKey).

These are pure functions over a tidy signature table; no network, no SMILES.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd

PREFERRED_DOSE_UM = 10.0
PREFERRED_TIME_H = 24.0


def _distance(dose: object, time: object) -> tuple[float, float]:
    """Distance from the preferred (10 uM, 24 h) condition; never crashes.

    ``sig_info`` delivers ``pert_dose``/``pert_time`` as STRINGS (and sometimes blanks
    or junk), so coerce numerically first: a non-numeric/non-positive dose or a
    non-numeric time ranks LAST (large distance) instead of raising on ``dose > 0``.
    """
    dose_val = pd.to_numeric(dose, errors="coerce")
    time_val = pd.to_numeric(time, errors="coerce")
    dose_ok = pd.notna(dose_val) and dose_val > 0
    dose_term = abs(math.log10(dose_val) - math.log10(PREFERRED_DOSE_UM)) if dose_ok else 1e9
    time_term = abs(float(time_val) - PREFERRED_TIME_H) if pd.notna(time_val) else 1e9
    return (dose_term, time_term)


def select_condition_signatures(sig_meta: pd.DataFrame) -> pd.DataFrame:
    """Pick ONE signature per (compound_key, cell_id) by the condition rule.

    ``sig_meta`` columns required: ``compound_key, sig_id, cell_id, pert_dose,
    pert_time`` (+ any feature columns, carried through). Returns the chosen rows.
    """
    ranked = sig_meta.copy()
    distances = ranked.apply(lambda r: _distance(r["pert_dose"], r["pert_time"]), axis=1)
    ranked["_d_dose"] = [d[0] for d in distances]
    ranked["_d_time"] = [d[1] for d in distances]
    ranked = ranked.sort_values(["compound_key", "cell_id", "_d_dose", "_d_time", "sig_id"])
    chosen = ranked.groupby(["compound_key", "cell_id"], as_index=False).first()
    return chosen.drop(columns=["_d_dose", "_d_time"])


def early_fuse(selected: pd.DataFrame, feature_cols: Sequence[str]) -> pd.DataFrame:
    """Mean MCF7+A549 landmark profiles -> one vector per compound (group=compound)."""
    fused = selected.groupby("compound_key", as_index=False)[list(feature_cols)].mean()
    return fused
