import itertools
import logging
import multiprocessing
import os
import pprint
import shutil
import traceback
from abc import ABC, abstractmethod
from collections import Counter
from typing import Tuple, Optional, Union, List, Callable, Any, TypeVar

import numpy as np
import openml
import pandas as pd
import sklearn.base
from imblearn.datasets import fetch_datasets
from imblearn.metrics import geometric_mean_score
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split as tts

from sklearn.metrics import *


from domain import Dataset
from utils.decorators import ExceptionWrapper

logger = logging.getLogger(__name__)
FittedModel = TypeVar('FittedModel', bound=Any)

class ExperimentRunner(ABC):
    def __init__(self, *args, **kwargs):
        self._tasks: List[Dataset, ...] = []
        self._id_counter = itertools.count(start=1)
        self._n_evals = 10
        self._fitted_model: FittedModel

        self._configure_environment()

    @abstractmethod
    def define_tasks(self, task_range: Optional[Tuple[int, ...]] = None):
        raise NotImplementedError()

    @abstractmethod
    def fit(
            self,
            X_train: Union[np.ndarray, pd.DataFrame],
            y_train: Union[np.ndarray, pd.Series],
            target_label: str,
            dataset_name: str):
        raise NotImplementedError()

    @abstractmethod
    def predict(self, X_test: Union[np.ndarray, pd.DataFrame]) -> np.ndarray:
        raise NotImplementedError()

    @abstractmethod
    def load_dataset(self, task_id: Optional[int] = None) -> Optional[Dataset]:
        raise NotImplementedError()

    def _log_val_loss_alongside_model_class(self, losses):
        for m, l in losses.items():
            logger.info(f"Validation loss: {float(l):.3f}")
            logger.info(pprint.pformat(f'Model class: {m}'))

    def get_tasks(self):
        return self._tasks

    def run(self, n_evals: Optional[int] = None):
        if n_evals is not None:
            self._n_evals = n_evals
        for task in self._tasks:
            if task is None:
                return

            logger.info(f"{task.id}...Loaded dataset name: {task.name}.")
            logger.info(f'N: {task.X.shape[0]}. M: {task.X.shape[1]}')

            if isinstance(task.X, np.ndarray):
                X_train, X_test, y_train, y_test = self.split_data_on_train_and_test(task.X, task.y)
            elif isinstance(task.X, pd.DataFrame):
                preprocessed_data = self.preprocess_data(task.X, task.y.squeeze())

                if preprocessed_data is None:
                    return

                X, y = preprocessed_data
                X_train, X_test, y_train, y_test = self.split_data_on_train_and_test(X, y.squeeze())

            class_belongings = Counter(y_train)
            logger.info(class_belongings)

            if len(class_belongings) > 2:
                logger.info("Multiclass problems are not currently supported.")
                return

            # is_dataset_initially_imbalanced = True

            iterator_of_class_belongings = iter(sorted(class_belongings))
            *_, positive_class_label = iterator_of_class_belongings
            number_of_positives = class_belongings.get(positive_class_label, None)

            if number_of_positives is None:
                logger.error("Unknown positive class label.")
                return

            proportion_of_positives = number_of_positives / len(y_train)

            # For extreme case - 0.01, for moderate - 0.2, for mild - 0.4.
            # if proportion_of_positives > 0.01:
            #     coefficient = 0.01
            #     updated_number_of_positives = int(coefficient * len(y_train))
            #     if len(str(updated_number_of_positives)) < 2:
            #         logger.info(f"Number of positive class instances is too low.")
            #         return
            #     class_belongings[positive_class_label] = updated_number_of_positives
            #     is_dataset_initially_imbalanced = False
            #
            # if not is_dataset_initially_imbalanced:
            #     X_train, y_train = make_imbalance(
            #         X_train,
            #         y_train,
            #         sampling_strategy=class_belongings)
            #     logger.info("Imbalancing applied.")

            number_of_train_instances_by_class = Counter(y_train)
            logger.info(number_of_train_instances_by_class)

            # estimated_dataset_size_in_memory = y_train.memory_usage(deep=True) / (1024 ** 2)
            # logger.info(f"Dataset size: {estimated_dataset_size_in_memory}")

            def fit_predict_evaluate():
                self.fit(X_train, y_train, task.target_label, task.name)
                y_predictions = self.predict(X_test)
                self.examine_quality(y_test, y_predictions, positive_class_label)
                # shutil.rmtree("/home/max/ray_results")
                # shutil.rmtree("/tmp/ray"
            ExceptionWrapper.log_exception(fit_predict_evaluate)()

    def examine_quality(self, y_test, y_pred, pos_label):
        f1 = fbeta_score(y_test, y_pred, beta=1, pos_label=pos_label)
        logger.info(f"F1: {f1:.3f}")

        balanced_accuracy = balanced_accuracy_score(y_test, y_pred)
        logger.info(f"Balanced accuracy: {balanced_accuracy:.3f}")

        recall = recall_score(y_test, y_pred, pos_label=pos_label)
        logger.info(f"Recall: {recall:.3f}")

        precision = precision_score(y_test, y_pred, pos_label=pos_label)
        logger.info(f"Precision: {precision:.3f}")

        gmean = geometric_mean_score(y_test, y_pred, pos_label=pos_label)
        logger.info(f"G-Mean: {gmean:.3f}")

        kappa = cohen_kappa_score(y_test, y_pred)
        logger.info(f"Kappa: {kappa:.3f}")

    def _configure_environment(self):
        openml.config.set_root_cache_directory("./openml_cache")

        np.random.seed(42)

        os.environ['RAY_IGNORE_UNHANDLED_ERRORS'] = '1'
        os.environ['TUNE_DISABLE_AUTO_CALLBACK_LOGGERS'] = '1'
        os.environ['TUNE_MAX_PENDING_TRIALS_PG'] = '1'

        logger.info("Prepared env.")

    def preprocess_data(self, X: pd.DataFrame, y: pd.Series) -> Optional[Tuple[pd.DataFrame, pd.Series]]:
        X.dropna(inplace=True)

        if type(y.iloc[0]) is str:
            label_encoder = LabelEncoder()
            y = pd.Series(label_encoder.fit_transform(y))

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


class ZenodoExperimentRunner(ExperimentRunner):
    def __init__(self):
        super().__init__()
        self.__datasets = fetch_datasets(data_home='datasets/imbalanced', verbose=True)

    def load_dataset(self, task_id: Optional[int] = None) -> Optional[Dataset]:
        for i, (dataset_name, dataset_data) in enumerate(self.__datasets.items()):
            # logger.info(i)
            if i + 1 == task_id:
                return Dataset(id=next(self._id_counter), name=dataset_name, X=dataset_data.get('data'), y=dataset_data.get('target'))
            # logger.info(f'i {i}, dataset name {dataset}')

    def define_tasks(self, task_range: Optional[Tuple[int, ...]] = None):
        if task_range is None:
            task_range = tuple(range(1, len(self.__datasets.keys())))
            logger.info(task_range)
        for i in task_range:
            self._tasks.append(self.load_dataset(i))


class OpenMLExperimentRunner(ExperimentRunner):
    def __init__(self):
        super().__init__()

    def load_dataset(self, task_id: Optional[int] = None) -> Optional[Dataset]:
        try:
            with multiprocessing.Pool(processes=1) as pool:
                task = pool.apply_async(openml.tasks.get_task, [task_id]).get(timeout=1800)
                dataset = pool.apply_async(task.get_dataset, []).get(timeout=1800)
            X, y, categorical_indicator, dataset_feature_names = dataset.get_data(
                target=dataset.default_target_attribute)

        except multiprocessing.TimeoutError:
            logger.error(f"Fetch from OpenML timed out. Dataset id={task_id} was not loaded.")
            return None
        except Exception as exc:
            logger.error(pprint.pformat(traceback.format_exception(type(exc), exc, exc.__traceback__   )))
            return None

        return Dataset(id=next(self._id_counter), name=dataset.name, target_label=dataset.default_target_attribute, X=X, y=y)

    def define_tasks(self, task_range: Tuple[int, ...] = None):
        self._tasks = []
        benchmark_suite = openml.study.get_suite(suite_id=271)

        for i, task_id in enumerate(benchmark_suite.tasks):
            # if iteration not in (5, 6, 7, 13, 14, 17, 61, 62, 69):
            if task_range is not None and i not in task_range:
                continue

            self._tasks.append(self.load_dataset(task_id))


