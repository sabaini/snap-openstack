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
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.commands.upgrades.inter_channel import ChannelUpgradeCoordinator
from sunbeam.commands.upgrades.intra_channel import LatestInChannelCoordinator
from sunbeam.jobs.common import run_plan
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import AddManifestStep, Manifest

LOG = logging.getLogger(__name__)
console = Console()
snap = Snap()


@click.command()
@click.option(
    "-c",
    "--clear-manifest",
    is_flag=True,
    default=False,
    help="Clear the manifest file.",
    type=bool,
)
@click.option(
    "-m",
    "--manifest",
    help="Manifest file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--upgrade-release",
    is_flag=True,
    show_default=True,
    default=False,
    help="Upgrade OpenStack release.",
)
@click.pass_context
def refresh(
    ctx: click.Context,
    upgrade_release: bool,
    manifest: Optional[Path] = None,
    clear_manifest: bool = False,
) -> None:
    """Refresh deployment.

    Refresh the deployment. If --upgrade-release is supplied then charms are
    upgraded the channels aligned with this snap revision
    """
    if clear_manifest and manifest:
        raise click.ClickException(
            "Options manifest and clear_manifest are mutually exclusive"
        )

    client: Client = ctx.obj

    # Validate manifest file
    manifest_obj = None
    if clear_manifest:
        run_plan([AddManifestStep(client)], console)
    elif manifest:
        manifest_obj = Manifest.load(
            client, manifest_file=manifest, include_defaults=True
        )
        run_plan([AddManifestStep(client, manifest)], console)

    if not manifest_obj:
        LOG.debug("Getting latest manifest from cluster db")
        manifest_obj = Manifest.load_latest_from_clusterdb(
            client, include_defaults=True
        )

    LOG.debug(f"Manifest used for deployment - software: {manifest_obj.software}")
    data_location = snap.paths.user_data
    jhelper = JujuHelper(client, data_location)
    if upgrade_release:
        a = ChannelUpgradeCoordinator(client, jhelper, manifest_obj)
        a.run_plan()
    else:
        a = LatestInChannelCoordinator(client, jhelper, manifest_obj)
        a.run_plan()
    click.echo("Refresh complete.")
