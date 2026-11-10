# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration tests that run the Workflows samples live.

These tests are skipped unless RUN_INTEGRATION_TESTS is "true". They need:
    GOOGLE_CLOUD_PROJECT: Project that runs the tests.
    SERVICE_ACCOUNT_EMAIL: Service account that test workflows run as.
    GOOGLE_CLOUD_LOCATION: Region for the workflows (default 'us-central1').
"""

import os
import pathlib
import time
from typing import Iterator
import uuid

from google.api_core import exceptions
from google.cloud import workflows_v1
from google.cloud.workflows import executions_v1
import pytest

import cancel_execution  # noqa: I100
import create_workflow  # noqa: I100
import delete_workflow  # noqa: I100
import execute_with_arguments  # noqa: I100
import execute_without_arguments  # noqa: I100
import get_execution  # noqa: I100
import get_workflow  # noqa: I100
import list_executions  # noqa: I100
import list_workflows  # noqa: I100
import main  # noqa: I100
import update_workflow  # noqa: I100

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "true",
    reason="Set RUN_INTEGRATION_TESTS=true to run integration tests.",
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION_ID = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL", "")
MY_FIRST_WORKFLOW = (
    pathlib.Path(__file__).resolve().parent.parent / "myFirstWorkflow.workflows.yaml"
).read_text()
SLEEP_WORKFLOW = """
main:
  steps:
    - wait:
        call: sys.sleep
        args:
          seconds: 300
"""
FAILING_WORKFLOW = """
main:
  steps:
    - fail:
        raise: "example-failure"
"""


@pytest.fixture(scope="module")
def workflows_client() -> workflows_v1.WorkflowsClient:
    return workflows_v1.WorkflowsClient()


@pytest.fixture(scope="module")
def executions_client() -> executions_v1.ExecutionsClient:
    return executions_v1.ExecutionsClient()


def deploy(client: workflows_v1.WorkflowsClient, source: str) -> Iterator[str]:
    """Deploys a test workflow with a unique ID and deletes it afterward."""
    workflow_id = f"test-workflow-{uuid.uuid4().hex[:12]}"
    client.create_workflow(
        parent=f"projects/{PROJECT_ID}/locations/{LOCATION_ID}",
        workflow=workflows_v1.Workflow(
            source_contents=source, service_account=SERVICE_ACCOUNT_EMAIL
        ),
        workflow_id=workflow_id,
    ).result(timeout=300)
    yield workflow_id
    try:
        client.delete_workflow(
            name=f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{workflow_id}"
        ).result(timeout=300)
    except exceptions.NotFound:
        pass


@pytest.fixture(scope="module")
def my_first_workflow_id(
    workflows_client: workflows_v1.WorkflowsClient,
) -> Iterator[str]:
    yield from deploy(workflows_client, MY_FIRST_WORKFLOW)


@pytest.fixture
def sleep_workflow_id(workflows_client: workflows_v1.WorkflowsClient) -> Iterator[str]:
    yield from deploy(workflows_client, SLEEP_WORKFLOW)


@pytest.fixture
def failing_workflow_id(
    workflows_client: workflows_v1.WorkflowsClient,
) -> Iterator[str]:
    yield from deploy(workflows_client, FAILING_WORKFLOW)


@pytest.fixture
def new_workflow_id(workflows_client: workflows_v1.WorkflowsClient) -> Iterator[str]:
    """Provides an unused workflow ID and deletes the workflow afterward."""
    workflow_id = f"test-workflow-{uuid.uuid4().hex[:12]}"
    yield workflow_id
    try:
        workflows_client.delete_workflow(
            name=f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{workflow_id}"
        ).result(timeout=300)
    except exceptions.NotFound:
        pass


def test_workflow_lifecycle(
    capsys: pytest.CaptureFixture[str], new_workflow_id: str
) -> None:
    args = (PROJECT_ID, LOCATION_ID, new_workflow_id)
    name = f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{new_workflow_id}"

    create_workflow.create_workflow(*args, SERVICE_ACCOUNT_EMAIL)
    out, err = capsys.readouterr()
    assert f"Resource ID: {name}" in out
    assert "Call Log Level: LOG_ERRORS_ONLY" in out
    assert "error:" not in err

    get_workflow.get_workflow(*args)
    assert f"Service Account: projects/{PROJECT_ID}/serviceAccounts/" in (
        capsys.readouterr().out
    )

    update_workflow.update_workflow(*args)
    assert "Description: example-description" in capsys.readouterr().out

    list_workflows.list_workflows(PROJECT_ID, LOCATION_ID)
    assert f"Resource ID: {name}" in capsys.readouterr().out

    delete_workflow.delete_workflow(*args)
    assert "State: DELETED" in capsys.readouterr().out

    get_workflow.get_workflow(*args)
    assert f"error: workflow '{new_workflow_id}' not found" in capsys.readouterr().err


def test_created_workflow_reads_its_environment_variable(
    capsys: pytest.CaptureFixture[str], new_workflow_id: str
) -> None:
    create_workflow.create_workflow(
        PROJECT_ID, LOCATION_ID, new_workflow_id, SERVICE_ACCOUNT_EMAIL
    )
    capsys.readouterr()

    execute_without_arguments.execute_workflow_without_arguments(
        PROJECT_ID, LOCATION_ID, new_workflow_id
    )

    out = capsys.readouterr().out
    assert 'Result: "example-greeting"' in out
    assert "State: SUCCEEDED" in out


@pytest.mark.parametrize(
    "run",
    [
        execute_without_arguments.execute_workflow_without_arguments,
        main.execute_workflow,
    ],
)
def test_execute_my_first_workflow(
    capsys: pytest.CaptureFixture[str], my_first_workflow_id: str, run: object
) -> None:
    run(PROJECT_ID, LOCATION_ID, my_first_workflow_id)

    out, err = capsys.readouterr()
    assert "State: SUCCEEDED" in out
    assert "error:" not in err


def test_execute_with_arguments(
    capsys: pytest.CaptureFixture[str], my_first_workflow_id: str
) -> None:
    execute_with_arguments.execute_workflow_with_arguments(
        PROJECT_ID, LOCATION_ID, my_first_workflow_id
    )

    out = capsys.readouterr().out
    assert "State: SUCCEEDED" in out
    assert "Cloud" in out


def test_get_execution(
    capsys: pytest.CaptureFixture[str],
    executions_client: executions_v1.ExecutionsClient,
    my_first_workflow_id: str,
) -> None:
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{my_first_workflow_id}"
    execution = executions_client.create_execution(parent=parent)
    execution_id = execution.name.rsplit("/", 1)[-1]

    get_execution.get_execution(
        PROJECT_ID, LOCATION_ID, my_first_workflow_id, execution_id
    )

    out, err = capsys.readouterr()
    assert f"Resource ID: {execution.name}" in out
    assert "error:" not in err


def test_list_failed_executions(
    capsys: pytest.CaptureFixture[str],
    executions_client: executions_v1.ExecutionsClient,
    failing_workflow_id: str,
) -> None:
    parent = (
        f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{failing_workflow_id}"
    )
    execution = executions_client.create_execution(parent=parent)
    deadline = time.monotonic() + 120
    while execution.state == executions_v1.Execution.State.ACTIVE:
        assert time.monotonic() < deadline, "The execution didn't finish."
        time.sleep(2)
        execution = executions_client.get_execution(name=execution.name)

    list_executions.list_executions(PROJECT_ID, LOCATION_ID, failing_workflow_id)

    out = capsys.readouterr().out
    assert f"Resource ID: {execution.name}" in out
    assert "example-failure" in out


def test_cancel_execution(
    capsys: pytest.CaptureFixture[str],
    executions_client: executions_v1.ExecutionsClient,
    sleep_workflow_id: str,
) -> None:
    parent = (
        f"projects/{PROJECT_ID}/locations/{LOCATION_ID}/workflows/{sleep_workflow_id}"
    )
    execution = executions_client.create_execution(parent=parent)
    execution_id = execution.name.rsplit("/", 1)[-1]

    cancel_execution.cancel_execution(
        PROJECT_ID, LOCATION_ID, sleep_workflow_id, execution_id
    )

    assert "State: CANCELLED" in capsys.readouterr().out
