# Copyright (c) 2023 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

import click
from rich.console import Console
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.commands.openstack import ResizeControlPlaneStep
from sunbeam.commands.terraform import TerraformInitStep
from sunbeam.jobs.common import click_option_topology, run_plan
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)
console = Console()
snap = Snap()


@click.command()
@click_option_topology
@click.option(
    "-f", "--force", help="Force resizing to incompatible topology.", is_flag=True
)
@click.pass_context
def resize(ctx: click.Context, topology: str, force: bool = False) -> None:
    """Expand the control plane to fit available nodes."""
    client: Client = ctx.obj
    manifest_obj = Manifest.load_latest_from_clusterdb(client, include_defaults=True)

    tfplan = "openstack-plan"
    data_location = snap.paths.user_data
    jhelper = JujuHelper(client, data_location)
    plan = [
        TerraformInitStep(manifest_obj.get_tfhelper(tfplan)),
        ResizeControlPlaneStep(client, manifest_obj, jhelper, topology, force),
    ]

    run_plan(plan, console)

    click.echo("Resize complete.")
