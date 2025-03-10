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
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import click
from packaging.version import Version
from pydantic import Field
from pydantic.dataclasses import dataclass
from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ClusterServiceUnavailableException
from sunbeam.commands.configure import retrieve_admin_credentials
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.commands.terraform import TerraformException, TerraformInitStep
from sunbeam.jobs.common import BaseStep, Result, ResultType, run_plan
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import Manifest, SoftwareConfig
from sunbeam.plugins.interface.v1.base import PluginRequirement
from sunbeam.plugins.interface.v1.openstack import (
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)
from sunbeam.versions import OPENSTACK_CHANNEL

LOG = logging.getLogger(__name__)
console = Console()


@dataclass
class CaasConfig:
    image_name: Optional[str] = Field(default=None, description="CAAS Image name")
    image_url: Optional[str] = Field(
        default=None, description="CAAS Image URL to upload to glance"
    )
    container_format: Optional[str] = Field(
        default=None, description="Image container format"
    )
    disk_format: Optional[str] = Field(default=None, description="Image disk format")
    properties: dict = Field(
        default={}, description="Properties to set for image in glance"
    )


class CaasConfigureStep(BaseStep):
    """Configure CaaS service."""

    def __init__(
        self,
        manifest: Manifest,
        tfplan: str,
        tfvar_map: dict,
    ):
        super().__init__(
            "Configure Container as a Service",
            "Configure Cloud for Container as a Service use",
        )
        self.manifest = manifest
        self.tfplan = tfplan
        self.tfvar_map = tfvar_map

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        try:
            override_tfvars = {}
            try:
                manifest_caas_config = asdict(self.manifest.software.caas_config)
                for caas_config_attribute, tfvar_name in (
                    self.tfvar_map.get(self.tfplan, {}).get("caas_config", {}).items()
                ):
                    caas_config_attribute_ = manifest_caas_config.get(
                        caas_config_attribute
                    )
                    if caas_config_attribute_:
                        override_tfvars[tfvar_name] = caas_config_attribute_
            except AttributeError:
                # caas_config not defined in manifest, ignore
                pass

            self.manifest.update_tfvars_and_apply_tf(
                tfplan=self.tfplan, override_tfvars=override_tfvars
            )
        except TerraformException as e:
            LOG.exception("Error configuring Container as a Service plugin.")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class CaasPlugin(OpenStackControlPlanePlugin):
    version = Version("0.0.1")
    requires = {
        PluginRequirement("secrets"),
        PluginRequirement("orchestration"),
        PluginRequirement("loadbalancer", optional=True),
    }

    def __init__(self, client: Client) -> None:
        super().__init__(
            "caas",
            client,
            tf_plan_location=TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO,
        )
        self.configure_plan = "caas-setup"

    def manifest_defaults(self) -> dict:
        """Manifest plugin part in dict format."""
        return {
            "charms": {
                "magnum-k8s": {"channel": OPENSTACK_CHANNEL},
            },
            "terraform": {
                self.configure_plan: {
                    "source": Path(__file__).parent / "etc" / self.configure_plan
                },
            },
        }

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan: {
                "charms": {
                    "magnum-k8s": {
                        "channel": "magnum-channel",
                        "revision": "magnum-revision",
                        "config": "magnum-config",
                    }
                }
            },
            self.configure_plan: {
                "caas_config": {
                    "image_name": "image-name",
                    "image_url": "image-source-url",
                    "container_format": "image-container-format",
                    "disk_format": "image-disk-format",
                    "properties": "image-properties",
                }
            },
        }

    def add_manifest_section(self, software_config: SoftwareConfig) -> None:
        """Adds manifest section"""
        try:
            _caas_config = software_config.caas_config
            software_config.caas_config = CaasConfig(**_caas_config)
        except AttributeError:
            # Attribute not defined in manifest
            software_config.caas_config = CaasConfig()

    def set_application_names(self) -> list:
        """Application names handled by the terraform plan."""
        apps = ["magnum", "magnum-mysql-router"]
        if self.get_database_topology() == "multi":
            apps.extend(["magnum-mysql"])

        return apps

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        return {
            "enable-magnum": True,
            **self.add_horizon_plugin_to_tfvars("magnum"),
        }

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {
            "enable-magnum": False,
            **self.remove_horizon_plugin_from_tfvars("magnum"),
        }

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    @click.command()
    def enable_plugin(self) -> None:
        """Enable Container as a Service plugin."""
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        """Disable Container as a Service plugin."""
        super().disable_plugin()

    @click.command()
    def configure(self):
        """Configure Cloud for Container as a Service use."""
        data_location = self.snap.paths.user_data
        jhelper = JujuHelper(self.client, data_location)
        admin_credentials = retrieve_admin_credentials(jhelper, OPENSTACK_MODEL)

        tfhelper = self.manifest.get_tfhelper(self.configure_plan)
        tfhelper.env = admin_credentials
        plan = [
            TerraformInitStep(tfhelper),
            CaasConfigureStep(
                self.manifest, self.configure_plan, self.manifest_attributes_tfvar_map()
            ),
        ]

        run_plan(plan, console)

    def commands(self) -> dict:
        """Dict of clickgroup along with commands."""
        commands = super().commands()
        try:
            enabled = self.enabled
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for plugin status, is cloud bootstrapped ?",
                exc_info=True,
            )
            enabled = False

        if enabled:
            commands.update(
                {
                    "configure": [{"name": "caas", "command": self.configure}],
                }
            )
        return commands
