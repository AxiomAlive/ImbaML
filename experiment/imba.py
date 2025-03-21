import logging
from typing import Union

import pandas    as pd
import numpy as np

from hyperopt import STATUS_OK, hp

from ray.tune import Tuner
from ray.tune.search import ConcurrencyLimiter
from sklearn.exceptions import NotFittedError
from sklearn.model_selection import cross_val_score, StratifiedKFold
from imbens.ensemble import AdaUBoostClassifier, AdaCostClassifier, AsymBoostClassifier
from sklearn.metrics import *

from search_spaces.ensemble.boost import AdaReweightedGenerator, XGBoostGenerator
from search_spaces.ensemble.bag import BalancedBaggingClassifierGenerator
from search_spaces.ensemble.bag import BalancedRandomForestGenerator
from utils.decorators import ExceptionWrapper

import ray
from ray.tune.search.hyperopt import HyperOptSearch
from ray.train import RunConfig

from .runner import ZenodoExperimentRunner, AutoMLRunner


logger = logging.getLogger(__name__)

class RayTuner:
    @staticmethod
    def trainable(config):
        trial_result = ImbaExperimentRunner.compute_metric_score(
            config['algorithm_configuration'],
            config['metric'],
            config['X'],
            config['y'])
        ray.train.report(trial_result)


#TODO: use ray.tune.Trainable directly
#
# class RayTrainable(ray.tune.Trainable):
#     def setup(self, config):
#         self.algorithm_configuration = config["algorithm_configuration"]
#         self.metric = config["metric"]
#         self.X = config['X']
#         self.y = config['y']
#
#     def step(self):
#         trial_result = ImbaExperimentRunner.compute_metric_score(
#             self.algorithm_configuration,
#             self.metric,
#             self.X,
#             self.y)
#         return {"loss": trial_result['loss']}


class ImbaExperimentRunner(AutoMLRunner):
    def __init__(self, metric):
        super().__init__(metric)

    @classmethod
    def compute_metric_score(cls, hyper_parameters, metric, X, y):
        hyper_parameters = hyper_parameters.copy()
        model_class = hyper_parameters.pop('model_class')
        clf = model_class(**hyper_parameters)

        loss_value = cross_val_score(
            estimator=clf,
            X=X,
            y=y,
            cv=StratifiedKFold(n_splits=8),
            scoring=make_scorer(metric),
            error_score='raise').mean()

        return {'loss': -loss_value, 'status': STATUS_OK}

    @ExceptionWrapper.log_exception
    def fit(
            self,
            X_train: Union[np.ndarray, pd.DataFrame],
            y_train: Union[np.ndarray, pd.Series],
            target_label: str,
            dataset_name: str,
            n_evals: int) -> None:
        logger.info(f"Number of optimization search trials: {n_evals}.")

        #TODO: check efficiency of EasyEnsemble and RUSBoost.
        # model_classes = [
        #     AdaReweightedGenerator.generate_algorithm_configuration_space(AdaUBoostClassifier),
        #     AdaReweightedGenerator.generate_algorithm_configuration_space(AdaCostClassifier),
        #     AdaReweightedGenerator.generate_algorithm_configuration_space(AsymBoostClassifier),
        #     BalancedRandomForestGenerator.generate_algorithm_configuration_space(),
        #     BalancedBaggingClassifierGenerator.generate_algorithm_configuration_space(),
        # ]
        model_classes = [
            XGBoostGenerator.generate_algorithm_configuration_space()
        ]

        algorithms_configuration = hp.choice("algorithm_configuration", model_classes)

        if self._metric == 'f1':
            metric = f1_score
        elif self._metric == 'balanced_accuracy':
            metric = balanced_accuracy_score
        elif self._metric == 'average_precision':
            metric = average_precision_score
        else:
            raise ValueError(f"_metric attribute contains not supported value: {self._metric}.")

        ray_configuration = {
            'X': X_train,
            'y': y_train,
            'metric': metric,
            'algorithm_configuration': algorithms_configuration
        }

        # HyperOptSearch(points_to_evaluate = promising initial points)
        search_algo = ConcurrencyLimiter(
            HyperOptSearch(
                space=ray_configuration,
                metric='loss',
                mode='min'),
            max_concurrent=5,
            batch=True)

        tuner = ray.tune.Tuner(
            RayTuner.trainable,
            tune_config=ray.tune.TuneConfig(
                metric='loss',
                mode='min',
                search_alg=search_algo,
                num_samples=n_evals),
            # run_config=ray.train.RunConfig(
            #     stop={"training_iteration": 1},
            #     checkpoint_config=ray.train.CheckpointConfig(
            #         checkpoint_at_end=False
            #     )
            # )
                # reuse_actors=True),
            # param_space=ray_configuration
        )

        logger.info(f"Document {dataset_name}")

        results = tuner.fit()

        best_trial = results.get_best_result(metric='loss', mode='min')

        best_trial_metrics = getattr(best_trial, 'metrics')
        if best_trial_metrics is None:
            raise Exception("Optimization failed. No best trial found.")

        logger.info(f"Training on dataset {dataset_name} finished.")

        best_validation_loss = best_trial_metrics.get('loss')

        best_algorithm_configuration = best_trial_metrics.get('config').get('algorithm_configuration')

        best_model_class = best_algorithm_configuration.get('model_class')
        best_algorithm_configuration.pop('model_class')

        best_model = best_model_class(**best_algorithm_configuration)

        val_losses = {best_model: best_validation_loss}
        self._log_val_loss_alongside_model_class(val_losses)

        best_model.fit(X_train, y_train)

        self._fitted_model = best_model

    @ExceptionWrapper.log_exception
    def predict(self, X_test):
        if self._fitted_model is None:
            raise NotFittedError()

        predictions = self._fitted_model.predict(X_test)
        return predictions
