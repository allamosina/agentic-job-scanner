import json

import pytest

from job_monitor.config import (
    PrivateConfigError,
    Settings,
    Source,
    fingerprint,
    load_preferences,
    load_sources,
)
from job_monitor.worker import watchlist_sources


def update(**changes):
    return json.dumps(dict(watchlists={'targets': ['Example Studio', 'Example AI']},
                          policy_text='Allow evidence-based adjacent transitions.', **changes))


def test_private_search_update_preserves_salary_cv_settings_and_schedule():
    original = load_preferences(Settings())
    changed = load_preferences(Settings(private_search_config_json=update()))
    assert changed.compensation_czk == original.compensation_czk
    assert changed.weights == original.weights
    assert changed.schedule == original.schedule
    assert changed.watchlists == {'targets': ['Example Studio', 'Example AI']}
    assert 'Allow evidence-based adjacent transitions.' in changed.policy_text
    assert 'Allow evidence-based adjacent transitions.' in changed.clarifications_text
    assert fingerprint(original) != fingerprint(changed)
    assert load_preferences(Settings()) == original


@pytest.mark.parametrize('value', ['not JSON PRIVATE_SENTINEL',
    '{"watchlists": {}, "policy_text": 42}',
    '{"watchlists": {}, "policy_text": "ok", "candidate": "PRIVATE_SENTINEL"}'])
def test_invalid_update_redacts_private_values(value):
    settings = Settings(private_search_config_json=value)
    assert value not in repr(settings)
    with pytest.raises(PrivateConfigError) as error:
        load_preferences(settings)
    assert error.value.code == 'invalid_search_config'
    assert 'PRIVATE_SENTINEL' not in str(error.value)


def test_sources_merge_and_jsonld_keeps_search_fallback():
    settings = Settings(private_search_config_json=update(sources=[
        dict(id='example-web', kind='jsonld', company='Example Studio', url='https://example.com/careers'),
        dict(id='example-ats', kind='ashby', company='Example AI', board='example')]))
    sources = load_sources(settings)
    prefs = load_preferences(settings)
    prefs.watchlists['duplicate'] = ['EXAMPLE STUDIO']
    discovery = watchlist_sources(prefs, sources)
    assert len(discovery) == 1
    assert discovery[0].company.casefold() == 'example studio'
    assert 'strategy' in discovery[0].query
    assert len(sources) == 2


def test_duplicate_update_source_ids_rejected():
    source = Source(id='same', kind='search', query='synthetic').model_dump()
    with pytest.raises(ValueError, match='Duplicate source IDs'):
        load_sources(Settings(private_search_config_json=update(sources=[source, source])))
