import os

# Tests always run in fixture mode, even if a .env with a live token exists.
os.environ["HUBSPOT_MODE"] = "fixtures"
os.environ.pop("HUBSPOT_PROFILE", None)
os.environ.pop("HUBSPOT_AS_OF", None)

import pytest  # noqa: E402

from hubspot_pipeline_mcp.crm import load_fixture  # noqa: E402


@pytest.fixture(scope="session")
def crm():
    return load_fixture()


@pytest.fixture
def anyio_backend():
    return "asyncio"
