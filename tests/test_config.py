from pathlib import Path

import pytest
import yaml

from giskard_wb_api.config import GpuOption, StorageLocation, load_config
from giskard_wb_api.server import main

from .conftest import TEST_CONFIG, make_config, merged

EXAMPLE = Path(__file__).parent.parent / 'config.example.yaml'


def write_config(tmp_path, doc) -> Path:
    path = tmp_path / 'config.yaml'
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc, sort_keys=False))
    return path


def without_credentials() -> dict:
    minio = {k: v for k, v in TEST_CONFIG['minio'].items() if not k.endswith('_key')}
    return {**TEST_CONFIG, 'minio': minio}


def test_loads_a_yaml_file_and_applies_defaults(tmp_path):
    doc = merged(
        TEST_CONFIG,
        {
            'server': {'port': 9090},
            'oidc': {'require_at_jwt_type': True},
            'storage': {'allowed_dataset_locations': ['giskard-datasets/public', 's3://giskard-datasets/team/']},
            'skypilot': {'api_server_endpoint': 'http://skypilot.skypilot.svc:80'},
        },
    )
    config = load_config(write_config(tmp_path, doc))
    assert config.port == 9090
    assert config.host == '0.0.0.0'
    assert config.oidc.require_at_jwt_type is True
    assert config.oidc.algorithms == ('RS256', 'PS256', 'ES256')
    assert config.allowed_dataset_locations == (
        StorageLocation('giskard-datasets', 'public/'),
        StorageLocation('giskard-datasets', 'team/'),
    )
    assert config.gpus == (GpuOption('H100', (1, 8)), GpuOption('A10', (1,)))
    assert config.sky.api_server_endpoint == 'http://skypilot.skypilot.svc:80'
    assert config.sky.threads == 8
    # Job credentials and endpoints fall back to the server's.
    assert config.minio.job_secret_key == 'server-secret-key-value'
    assert config.minio.job_endpoint == 'https://minio.example.com'


def test_reads_credentials_from_files_in_secrets_dir(tmp_path):
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    (secrets / 'minio.access_key').write_text('file-access-key\n')
    (secrets / 'minio.secret_key').write_text('file-secret-key\n')
    (secrets / 'skypilot.service_account_token').write_text('sky_token')
    # Kubernetes Secret volumes also contain ..data and timestamped directories.
    (secrets / '..data').mkdir()
    config = load_config(write_config(tmp_path, {**without_credentials(), 'secrets_dir': 'secrets'}))
    assert config.minio.access_key == 'file-access-key'
    assert config.minio.secret_key == 'file-secret-key'
    assert config.minio.job_access_key == 'file-access-key'
    assert config.sky.service_account_token == 'sky_token'


def test_rejects_a_credential_given_both_inline_and_as_a_file(tmp_path):
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    (secrets / 'minio.secret_key').write_text('file-secret-key')
    with pytest.raises(ValueError, match='minio.secret_key is set both'):
        load_config(write_config(tmp_path, {**TEST_CONFIG, 'secrets_dir': str(secrets)}))


def test_rejects_a_missing_secrets_dir(tmp_path):
    with pytest.raises(ValueError, match='not a directory'):
        load_config(write_config(tmp_path, {**TEST_CONFIG, 'secrets_dir': 'nope'}))


def test_reports_every_missing_required_setting():
    with pytest.raises(ValueError) as err:
        make_config({'oidc': {'issuer': ''}, 'minio': {'secret_key': None}, 'storage': {'allowed_trainer_locations': []}})
    message = str(err.value)
    assert 'oidc.issuer' in message
    assert 'minio.secret_key: Field required' in message
    assert 'storage.allowed_trainer_locations' in message


def test_rejects_unknown_keys_so_typos_fail_at_startup():
    with pytest.raises(ValueError, match=r'oidc\.admin_rol: Extra inputs are not permitted'):
        make_config({'oidc': {'admin_rol': 'giskard-admin'}})


@pytest.mark.parametrize(
    'override, message',
    [
        ({'server': {'port': 'eighty'}}, 'server.port'),
        ({'runs': {'gpus': {'H100': [0]}}}, 'Invalid GPU counts'),
        ({'runs': {'gpus': {'H100': []}}}, 'Invalid GPU counts'),
        ({'runs': {'gpus': None}}, 'runs.gpus: Field required'),
        ({'storage': {'results_location': 'Bad_Bucket/runs'}}, 'Invalid bucket'),
        ({'minio': {'force_path_style': 'maybe'}}, 'minio.force_path_style'),
        ({'skypilot': {'threads': 0}}, 'skypilot.threads'),
        ({'runs': {'stop_grace_seconds': -1}}, 'runs.stop_grace_seconds'),
    ],
)
def test_rejects_invalid_values(override, message):
    with pytest.raises(ValueError, match=message):
        make_config(override)


def test_rejects_unreadable_files(tmp_path):
    with pytest.raises(ValueError, match='mapping'):
        load_config(write_config(tmp_path, '- a\n- b\n'))
    with pytest.raises(ValueError, match='Cannot read'):
        load_config(tmp_path / 'missing.yaml')


def test_the_example_config_is_valid():
    config = load_config(EXAMPLE)
    assert config.minio.secret_key


def test_the_command_line_reports_a_bad_config(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(['--config', str(tmp_path / 'missing.yaml')])
    assert exit_info.value.code == 2
    assert 'Cannot read config file' in capsys.readouterr().err
