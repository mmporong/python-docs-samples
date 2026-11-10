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

import pathlib
import runpy
from unittest import mock

from google.cloud import workflows_v1
import pytest

SAMPLE = str(pathlib.Path(__file__).resolve().parent.parent / "get_workflow.py")
CLIENT = "google.cloud.workflows_v1.WorkflowsClient"
ENV_VARS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "WORKFLOW_ID")


def run_entrypoint(monkeypatch: pytest.MonkeyPatch, *flags: str) -> str:
    """Runs get_workflow.py as a script and returns the workflow it requested."""
    monkeypatch.setattr("sys.argv", ["get_workflow.py", *flags])
    with mock.patch(CLIENT) as mock_client_class:
        client = mock_client_class.return_value
        client.get_workflow.return_value = workflows_v1.Workflow(name="workflow")
        runpy.run_path(SAMPLE, run_name="__main__")
    return client.get_workflow.call_args.kwargs["name"]


def test_entrypoint_reads_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var, value in zip(ENV_VARS, ("env-project", "europe-west1", "env-wf")):
        monkeypatch.setenv(env_var, value)

    name = run_entrypoint(monkeypatch)

    assert name == "projects/env-project/locations/europe-west1/workflows/env-wf"


def test_entrypoint_flags_override_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var, value in zip(ENV_VARS, ("env-project", "europe-west1", "env-wf")):
        monkeypatch.setenv(env_var, value)

    name = run_entrypoint(monkeypatch, "--workflow-id", "flag-wf")

    assert name == "projects/env-project/locations/europe-west1/workflows/flag-wf"


def test_entrypoint_uses_example_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

    name = run_entrypoint(monkeypatch)

    assert name == (
        "projects/example-project-id/locations/us-central1/workflows/myFirstWorkflow"
    )
