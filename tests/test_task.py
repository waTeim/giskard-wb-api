import subprocess

import pytest

from giskard_wb_api.skypilot.task import RUN_SCRIPT, SETUP_SCRIPT, build_task

from .conftest import make_config

config = make_config({'skypilot': {'region': 'us-east-1'}})

run = {
    'id': 'run-1',
    'trainer': {'bucket': 'giskard-artifacts', 'key': 'wheels/$(touch pwned)-1.0-py3-none-any.whl', 'entrypoint': 'trainer'},
    'dataset': {'bucket': 'giskard-datasets', 'key': 'a/`id`.csv'},
    'compute': {'gpuType': 'A10', 'gpuCount': 1},
    'results': {'bucket': 'giskard-results', 'prefix': 'runs/run-1/outputs/'},
}


def test_keeps_the_scripts_constant_and_passes_run_values_only_as_environment_variables():
    _name, task = build_task(config, run, {'bucket': 'giskard-artifacts', 'key': 'configs/c.yaml'})
    assert task['setup'] == SETUP_SCRIPT
    assert task['run'] == RUN_SCRIPT
    assert task['resources'] == {'infra': 'lambda/us-east-1', 'accelerators': 'A10:1', 'disk_size': 256}
    assert task['envs'] | {
        'GISKARD_TRAINER_KEY': 'wheels/$(touch pwned)-1.0-py3-none-any.whl',
        'GISKARD_DATASET_KIND': 'object',
        'GISKARD_CONFIG_FILE': 'config.yaml',
        'GISKARD_MAX_RUNTIME_SECONDS': str(600 * 60),
    } == task['envs']


@pytest.mark.parametrize('script', [SETUP_SCRIPT, RUN_SCRIPT], ids=['setup', 'run'])
def test_produces_syntactically_valid_bash(script):
    subprocess.run(['bash', '-n'], input=script, text=True, check=True)
