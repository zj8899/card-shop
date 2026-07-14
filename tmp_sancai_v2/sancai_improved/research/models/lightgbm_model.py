"""LightGBM model wrapper for alpha prediction.

Supports both regression (predict forward return magnitude) and
classification (predict up/down direction).
"""
import logging

import numpy as np

from .interface import IModel

logger = logging.getLogger(__name__)


class LightGBMModel(IModel):
    """LightGBM wrapper implementing IModel.

    Parameters
    ----------
    task : str
        "regression" (default) or "classification".
    params : dict | None
        LightGBM hyperparameters. Sensible defaults are used when None.
    device : str
        "cpu" (default) or "gpu". GPU 需要 NVIDIA GPU + CUDA 驱动 ≥450。
        百万行+的特征矩阵建议用 gpu，千级规模用 cpu。
    """

    def __init__(self, task: str = "regression", params: dict | None = None,
                 device: str = "cpu"):
        self._task = task
        self._device = device
        self._params = params or self._default_params()
        self._model = None

    @staticmethod
    def _default_params() -> dict:
        return {
            "boosting_type": "gbdt",
            "objective": "regression",
            "metric": "rmse",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
            "n_estimators": 200,
            "num_threads": 2,
            "seed": 42,
        }

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LightGBMModel":
        import warnings
        import lightgbm as lgb

        X_arr = np.asarray(X, dtype=np.float64)
        y_arr = np.asarray(y, dtype=np.float64)

        params = {**self._params}
        if self._task == "classification":
            params["objective"] = "binary"
            params["metric"] = "auc"

        if self._device == "gpu":
            params["device"] = "gpu"
            params["gpu_platform_id"] = 0
            params["gpu_device_id"] = 0
            # 直方图最大 bin 数对 GPU 友好 (减少显存占用)
            if "max_bin" not in params:
                params["max_bin"] = 63

        self._model = lgb.LGBMRegressor(**params) if self._task == "regression" else lgb.LGBMClassifier(**params)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="X does not have valid feature names")
            self._model.fit(X_arr, y_arr)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("Model not fitted. Call fit() first.")
        raw = self._model.predict(X)
        return raw if isinstance(raw, np.ndarray) else np.array(raw)

    @property
    def feature_importances_(self) -> np.ndarray | None:
        """Feature importance scores (split-based) if fitted."""
        if self._model is None:
            return None
        return getattr(self._model, "feature_importances_", None)
