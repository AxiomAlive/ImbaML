import logging
import os
import pprint
import time
from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
from typing import Union, Optional, List, Tuple, final

import numpy as np
import pandas as pd
import ray
from imblearn.datasets import make_imbalance
from imblearn.metrics import geometric_mean_score
from sklearn.exceptions import NotFittedError
from sklearn.metrics import fbeta_score, balanced_accuracy_score, recall_score, precision_score, cohen_kappa_score, \
    precision_recall_curve, auc, average_precision_score
from sklearn.preprocessing import LabelEncoder

from experiment.repository import FittedModel, ZenodoExperimentRunner
from utils.decorators import ExceptionWrapper
from sklearn.model_selection import train_test_split as tts
from ray.tune import logger as ray_logger

logger = logging.getLogger(__name__)


class AutoMLRunner(ABC):
    def __init__(self, metrics):
        self._metrics = metrics
        self._benchmark_runner = ZenodoExperimentRunner()
        self._n_evals = 70
        self._fitted_model: FittedModel = None

        self._configure_environment()

    @property
    def benchmark_runner(self):
        return self._benchmark_runner

    def _configure_environment(self):
        np.random.seed(42)
        logger.info("Set seed to 42.")

        logger.info("Prepared environment.")

    @abstractmethod
    def fit(
            self,
            X_train: Union[np.ndarray, pd.DataFrame],
            y_train: Union[np.ndarray, pd.Series],
            metric_name: str,
            target_label: str,
            dataset_name: str):
        raise NotImplementedError()

    def predict(self, X_test: Union[np.ndarray, pd.DataFrame]) -> np.ndarray:
        if self._fitted_model is None:
            raise NotFittedError()

        predictions = self._fitted_model.predict(X_test)
        return predictions

    def _make_imbalance(self, X_train, y_train, class_belongings, pos_label) -> Tuple[Union[pd.DataFrame, np.ndarray], Union[pd.DataFrame, np.ndarray]]:
        is_dataset_initially_imbalanced = True
        number_of_positives = class_belongings.get(pos_label)

        proportion_of_positives = number_of_positives / len(y_train)

        # For extreme case - 0.01, for moderate - 0.2, for mild - 0.4.
        if proportion_of_positives > 0.01:
            coefficient = 0.01
            updated_number_of_positives = int(coefficient * len(y_train))
            if len(str(updated_number_of_positives)) < 2:
                logger.warning(f"Number of positive class instances is too low.")
            else:
                class_belongings[pos_label] = updated_number_of_positives
                is_dataset_initially_imbalanced = False

        if not is_dataset_initially_imbalanced:
            X_train, y_train = make_imbalance(
                X_train,
                y_train,
                sampling_strategy=class_belongings)
            logger.info("Imbalancing applied.")

        return X_train, y_train

    @final
    def _log_val_loss_alongside_model_class(self, losses):
        for m, l in losses.items():
            logger.info(f"Validation loss: {float(l):.3f}")
            logger.info(pprint.pformat(f'Model class: {m}'))

    @ExceptionWrapper.log_exception
    def run(self):
        for task in self._benchmark_runner.get_tasks():
            if task is None:
                return

            if isinstance(task.X, np.ndarray) or isinstance(task.X, pd.DataFrame):
                #     label_encoder = LabelEncoder()
                #     encoded_y = label_encoder.fit_transform(task.y)
                #     X_train, X_test, y_train, y_test = self.split_data_on_train_and_test(task.X, encoded_y)
                # elif isinstance(task.X, pd.DataFrame):
                preprocessed_data = self.preprocess_data(task.X, task.y.squeeze())

                if preprocessed_data is None:
                    return

                X, y = preprocessed_data
                X_train, X_test, y_train, y_test = self.split_data_on_train_and_test(X, y.squeeze())
            else:
                raise TypeError(f"pd.DataFrame or np.ndarray expected. Got: {type(task.X)}")

            logger.info(f"{task.id}...Loaded dataset name: {task.name}.")
            logger.info(f'Rows: {X_train.shape[0]}. Columns: {X_train.shape[1]}')

            class_belongings = Counter(y_train)
            logger.info(class_belongings)

            if len(class_belongings) > 2:
                logger.info("Multiclass problems currently not supported.")
                return

            iterator_of_class_belongings = iter(sorted(class_belongings))
            *_, positive_class_label = iterator_of_class_belongings
            logger.info(f"Positive class label: {positive_class_label}")

            number_of_positives = class_belongings.get(positive_class_label, None)

            if number_of_positives is None:
                logger.error("Unknown positive class label.")
                return

            number_of_train_instances_by_class = Counter(y_train)
            logger.info(number_of_train_instances_by_class)

            for metric in self._metrics:
                start_time = time.time()
                self.fit(X_train, y_train, metric, task.target_label, task.name)
                self.examine_quality('time_passed', start_time=start_time)

                y_predictions = self.predict(X_test)
                self.examine_quality(metric, y_test, y_predictions, positive_class_label)

    def _compute_metric_score(self, metric: str, *args, **kwargs):
        y_test = kwargs.get("y_test")
        y_pred = kwargs.get("y_pred")
        pos_label = kwargs.get("pos_label")
        start_time = kwargs.get("start_time")

        if metric == 'f1':
            f1 = fbeta_score(y_test, kwargs.get("y_pred"), beta=1, pos_label=pos_label)
            logger.info(f"F1: {f1:.3f}")
        elif metric == 'balanced_accuracy':
            balanced_accuracy = balanced_accuracy_score(y_test, y_pred)
            logger.info(f"Balanced accuracy: {balanced_accuracy:.3f}")
        elif metric == 'average_precision':
            average_precision = average_precision_score(y_test, y_pred, pos_label=pos_label)
            logger.info(f"Average precision: {average_precision:.3f}")
        elif metric == 'recall':
            recall = recall_score(y_test, y_pred, pos_label=pos_label)
            logger.info(f"Recall: {recall:.3f}")
        elif metric == 'precision':
            precision = precision_score(y_test, y_pred, pos_label=pos_label)
            logger.info(f"Precision: {precision:.3f}")
        elif metric == 'time_passed':
            time_passed = time.time() - start_time
            logger.info(f"Time passed: {time_passed // 60} minutes.")

    def examine_quality(
            self,
            metrics: Union[str, List[str]],
            y_test: Optional[Union[pd.DataFrame, np.ndarray]]=None,
            y_pred: Optional[Union[pd.DataFrame, np.ndarray]]=None,
            pos_label:Optional[int]=None,
            start_time:Optional[float]=None):

        compute_metric_score_kwargs = {
            'y_test': y_test,
            'y_pred': y_pred,
            'pos_label': pos_label,
            'start_time': start_time
        }

        # TODO: add handling for 'all' as a value of metrics to avoid hard-coding all metric names.
        if isinstance(metrics, str):
            self._compute_metric_score(
                metrics,
                **compute_metric_score_kwargs)
        elif isinstance(metrics, list):
            for metric in metrics:
                self._compute_metric_score(
                    metric,
                    **compute_metric_score_kwargs)

    def preprocess_data(self, X: Union[pd.DataFrame, np.ndarray], y: Union[pd.Series, np.ndarray]) -> Optional[Tuple[pd.DataFrame, pd.Series]]:
        if isinstance(X, pd.DataFrame):
            X.dropna(inplace=True)

        label_encoder = LabelEncoder()
        encoded_y = label_encoder.fit_transform(y)

        if isinstance(y, pd.Series):
            y = pd.Series(encoded_y)
        else:
            y = encoded_y

        if isinstance(X, pd.DataFrame):
            for dataset_feature_name in X.copy():
                dataset_feature = X.get(dataset_feature_name)

                if len(dataset_feature) == 0:
                    X.drop([dataset_feature_name], axis=1, inplace=True)
                    continue
                if type(dataset_feature.iloc[0]) is str:
                    dataset_feature_encoded = pd.get_dummies(dataset_feature, prefix=dataset_feature_name)
                    X.drop([dataset_feature_name], axis=1, inplace=True)
                    X = pd.concat([X, dataset_feature_encoded], axis=1).reset_index(drop=True)

            if len(X.index) != len(y.index):
                logger.warning(f"X index: {X.index} and y index {y.index}.")
                logger.error("Unexpected X size.")
                return None

        return X, y

    def split_data_on_train_and_test(self, X, y):
        return tts(
            X,
            y,
            random_state=42,
            test_size=0.2,
            stratify=y)
