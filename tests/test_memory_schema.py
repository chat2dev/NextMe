"""Tests for updated Fact schema and Settings.memory_max_facts."""
from datetime import datetime
from nextme.memory.schema import Fact
from nextme.config.schema import Settings


def test_fact_has_updated_at_field_defaulting_to_none():
    f = Fact(text="hello")
    assert f.updated_at is None


def test_fact_updated_at_can_be_set():
    now = datetime.now()
    f = Fact(text="hello", updated_at=now)
    assert f.updated_at == now


def test_settings_has_memory_max_facts_default_100():
    s = Settings()
    assert s.memory_max_facts == 100


def test_settings_memory_max_facts_configurable():
    s = Settings(memory_max_facts=50)
    assert s.memory_max_facts == 50
