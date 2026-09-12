"""Explicit user rules; reactions and application outcomes never invent rules."""
from sqlalchemy import select

from .config import load_preferences
from .models import State


def approved_rules(session, user_id):
    return [row for row in session.scalars(
        select(State).where(State.key.like(f'rule:{user_id}:%')).order_by(State.key)
    ) if row.value.get('status') == 'approved']


def runtime_preferences(factory, settings):
    prefs = load_preferences(settings)
    with factory() as session:
        rules = approved_rules(session, settings.telegram_user_id)
    if rules:
        text = '\n'.join('- ' + row.value['text'] for row in rules)
        prefs = prefs.model_copy(update={'policy_text': prefs.policy_text +
            '\nExplicit user-confirmed search preferences (do not change CV facts):\n' + text})
    return prefs
