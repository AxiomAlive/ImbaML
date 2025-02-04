import argparse
import logging
import os
import shutil
import sys
from datetime import datetime

from setuptools import setup

import numpy as np
from experiment.runner import OpenMLExperimentRunner


class ExperimentMain:
    @staticmethod
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument('--automl', action='store', dest='automl', default='imba')
        parser.add_argument('--out', action='store', dest='out', default='file')
        parser.add_argument('--preset', action='store', dest='preset', default='good_quality')
        parser.add_argument('--trials', action='store', dest='t', default=30)

        args = parser.parse_args()
        automl = getattr(args, 'automl')
        logging_output = getattr(args, 'o')
        autogluon_preset = getattr(args, 'preset')
        trials = getattr(args, 'trials')

        if logging_output == 'file':
            if automl == 'ag':
                log_file_name = 'logs/AG/'
            else:
                log_file_name = 'logs/Imba/'
            log_file_name += datetime.now().strftime('%Y-%m-%d %H:%M') + '.log'

            logging.basicConfig(
                filename=log_file_name,
                filemode="a",
                level=logging.INFO,
                format='%(asctime)s - %(levelname)s - %(message)s'
            )
        elif logging_output == 'console':
            logging.basicConfig(
                stream=sys.stdout,
                level=logging.INFO,
                format='%(asctime)s - %(levelname)s - %(message)s'
            )
        else:
            raise Exception("Invalid --o option. Options available: ['file', 'console'].")

        if automl == 'ag':
            if autogluon_preset not in ['medium_quality', 'good_quality']:
                raise Exception("Invalid --preset option. Options available: ['medium_quality', 'good_quality'].")

            from experiment.autogluon import AGExperimentRunner

            runner = AGExperimentRunner(autogluon_preset)
        elif automl == 'imba':
            from experiment.imba import ImbaExperimentRunner

            runner = ImbaExperimentRunner()
        else:
            raise Exception("Invalid --automl option. Options available: ['imba', 'ag'].")

        runner.define_tasks()

        runner.run(trials)




if __name__ == '__main__':
    ExperimentMain.main()

