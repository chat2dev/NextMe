"""Shared test fixtures."""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from nextme.config.schema import AppConfig, Project, Settings


@pytest.fixture
def settings():
    return Settings(
        memory_debounce_seconds=1,
        task_queue_capacity=10,
        progress_debounce_seconds=0.1,
        permission_timeout_seconds=1.0,
    )


@pytest.fixture
def project(tmp_path):
    return Project(name="test-proj", path=str(tmp_path), executor="claude-code-acp")


@pytest.fixture
def app_config(project):
    return AppConfig(app_id="cli_test", app_secret="secret", projects=[project])
