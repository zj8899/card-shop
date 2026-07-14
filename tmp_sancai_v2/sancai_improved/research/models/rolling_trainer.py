"""Rolling-window forward training.

Qlib-style: for each test time t, train on the preceding window,
predict for the next horizon, then evaluate out-of-sample.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .interface import IModel

logger = logging.getLogger(__name__)


@dataclass
class TrainResult:
    """Result from one train/test split."""
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    ic: float = 0.0
    rank_ic: float = 0.0
    mse: float = 0.0
    pred_mean: float = 0.0
    pred_std: float = 0.0


@dataclass
class RollingResult:
    """Aggregate result from a full rolling training run."""
    model_name: str
    results: list[TrainResult] = field(default_factory=list)
    all_preds: np.ndarray = field(default_factory=lambda: np.array([]))
    all_truth: np.ndarray = field(default_factory=lambda: np.array([]))

    @property
    def mean_ic(self) -> float:
        if not self.results:
            return 0.0
        return float(np.mean([r.ic for r in self.results]))

    @property
    def ic_ir(self) -> float:
        ics = [r.ic for r in self.results]
        if len(ics) < 2:
            return 0.0
        std = float(np.std(ics))
        return self.mean_ic / std if std > 0 else 0.0

    @property
    def overall_ic(self) -> float:
        if len(self.all_preds) < 10:
            return 0.0
        from scipy.stats import spearmanr
        corr, _ = spearmanr(self.all_preds, self.all_truth)
        return corr if not np.isnan(corr) else 0.0


class RollingTrainer:
    """Rolling-window forward training for time-series ML.

    Parameters
    ----------
    model : IModel
        Model instance with fit()/predict().
    window_size : int
        Training window in bars (e.g. 252 for one year of daily data).
    retrain_freq : int
        Retrain every N bars (1 = every bar, 20 = every month). Higher is faster.
    forward_periods : int
        How far ahead to predict (1 = next bar).
    min_train : int
        Minimum training bars before first prediction.
    device : str
        "cpu" (default) or "gpu". Passed to LightGBM via model params.
    """

    def __init__(
        self,
        model: IModel,
        window_size: int = 252,
        retrain_freq: int = 20,
        forward_periods: int = 1,
        min_train: int = 60,
        device: str = "cpu",
    ):
        self.model = model
        self.window_size = window_size
        self.retrain_freq = retrain_freq
        self.forward_periods = forward_periods
        self.min_train = min_train
        self.device = device

    def run(self, df: pd.DataFrame, feature_cols: list[str],
            target_col: str = "forward_return") -> RollingResult:
        """Run rolling training on OHLCV + factor DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            Must include feature_cols and target_col.
        feature_cols : list[str]
            Column names to use as model features.
        target_col : str
            Column name to predict (e.g. "forward_return" = next-bar pct change).

        Returns
        -------
        RollingResult with per-window metrics and aggregate predictions.
        """
        X_all = df[feature_cols].values
        y_all = df[target_col].values
        n = len(df)

        result = RollingResult(
            model_name=type(self.model).__name__,
            results=[],
            all_preds=np.full(n, np.nan),
            all_truth=y_all.copy(),
        )

        for test_start in range(self.min_train, n - self.forward_periods):
            # Only retrain every retrain_freq bars
            if (test_start - self.min_train) % self.retrain_freq != 0:
                continue

            train_start = max(0, test_start - self.window_size)
            train_end = test_start

            X_train = X_all[train_start:train_end]
            y_train = y_all[train_start:train_end]
            X_test = X_all[test_start:test_start + 1]
            y_test = y_all[test_start:test_start + 1]

            # Drop rows with NaN features or target
            train_mask = ~np.isnan(X_train).any(axis=1) & ~np.isnan(y_train)
            X_train = X_train[train_mask]
            y_train = y_train[train_mask]

            if len(X_train) < self.min_train:
                continue

            try:
                self.model.fit(X_train, y_train)
                pred = self.model.predict(X_test)
                result.all_preds[test_start] = pred[0]

                ic_val = self._calc_ic(pred, y_test)
                mse_val = float(np.mean((pred - y_test) ** 2))

                result.results.append(TrainResult(
                    train_start=str(df.index[train_start]),
                    train_end=str(df.index[train_end - 1]),
                    test_start=str(df.index[test_start]),
                    test_end=str(df.index[min(test_start + self.forward_periods - 1, n - 1)]),
                    n_train=len(X_train),
                    n_test=1,
                    ic=round(ic_val, 6),
                    rank_ic=round(ic_val, 6),
                    mse=round(mse_val, 6),
                    pred_mean=round(float(pred.mean()), 6),
                    pred_std=round(float(pred.std()) if len(pred) > 1 else 0.0, 6),
                ))
            except Exception as e:
                logger.warning("Train/predict failed at bar %d: %s", test_start, e)

        return result

    @staticmethod
    def _calc_ic(pred: np.ndarray, truth: np.ndarray) -> float:
        mask = ~np.isnan(pred) & ~np.isnan(truth)
        if mask.sum() < 10:
            return 0.0
        from scipy.stats import spearmanr
        corr, _ = spearmanr(pred[mask], truth[mask])
        return corr if not np.isnan(corr) else 0.0
