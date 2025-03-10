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

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Optional

from rich.status import Status
from snaphelpers import Snap

from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.jobs.common import BaseStep, Result, ResultType
from sunbeam.jobs.juju import JujuAccount, JujuController

LOG = logging.getLogger(__name__)

http_backend_template = """
terraform {
  backend "http" {
    address                = $address
    update_method          = $update_method
    lock_address           = $lock_address
    lock_method            = $lock_method
    unlock_address         = $unlock_address
    unlock_method          = $unlock_method
    skip_cert_verification = $skip_cert_verification
  }
}
"""

terraform_rc_template = """
disable_checkpoint = true
provider_installation {
  filesystem_mirror {
    path    = "$snap_path/usr/share/terraform-providers"
  }
}
"""


class TerraformException(Exception):
    """Terraform related exceptions"""

    def __init__(self, message):
        super().__init__()
        self.message = message

    def __str__(self) -> str:
        return self.message


class TerraformHelper:
    """Helper for interaction with Terraform"""

    def __init__(
        self,
        path: Path,
        plan: str,
        env: Optional[dict] = None,
        parallelism: Optional[int] = None,
        backend: Optional[str] = None,
        data_location: Optional[Path] = None,
    ):
        self.snap = Snap()
        self.path = path
        self.plan = plan
        self.env = env
        self.parallelism = parallelism
        self.backend = backend or "local"
        self.data_location = data_location
        self.terraform = str(self.snap.paths.snap / "bin" / "terraform")

    def backend_config(self) -> dict:
        if self.backend == "http":
            local_ip = utils.get_local_ip_by_default_route()
            local_address = f"https://{local_ip}:7000"
            return {
                "address": f"{local_address}/1.0/terraformstate/{self.plan}",
                "update_method": "PUT",
                "lock_address": f"{local_address}/1.0/terraformlock/{self.plan}",
                "lock_method": "PUT",
                "unlock_address": f"{local_address}/1.0/terraformunlock/{self.plan}",
                "unlock_method": "PUT",
                "skip_cert_verification": True,
            }
        return {}

    def write_backend_tf(self) -> None:
        backend = self.backend_config()
        if self.backend == "http":
            backend_obj = Template(http_backend_template)
            backend = backend_obj.safe_substitute(
                {key: json.dumps(value) for key, value in backend.items()}
            )

            with Path(self.path / "backend.tf").open(mode="w") as file:
                file.write(backend)

    def write_tfvars(self, vars: dict, location: Optional[Path] = None) -> None:
        """Write terraform variables file"""
        filepath = location or (self.path / "terraform.tfvars.json")
        with filepath.open("w") as tfvars:
            tfvars.write(json.dumps(vars))

    def write_terraformrc(self) -> None:
        """Write .terraformrc file"""
        terraform_rc = self.snap.paths.user_data / ".terraformrc"
        with terraform_rc.open(mode="w") as file:
            file.write(
                Template(terraform_rc_template).safe_substitute(
                    {"snap_path": self.snap.paths.snap}
                )
            )

    def update_juju_provider_credentials(self) -> dict:
        os_env = {}
        if self.data_location:
            LOG.debug("Updating terraform env variables related to juju credentials")
            account = JujuAccount.load(self.data_location)
            # TODO(gboutry): refactor when Manifest support lands
            controller = JujuController.load(Client.from_socket())
            os_env.update(
                JUJU_USERNAME=account.user,
                JUJU_PASSWORD=account.password,
                JUJU_CONTROLLER_ADDRESSES=",".join(controller.api_endpoints),
                JUJU_CA_CERT=controller.ca_cert,
            )

        return os_env

    def init(self) -> None:
        """terraform init"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-init-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        if self.backend:
            self.write_backend_tf()
        if self.data_location:
            os_env.update(self.update_juju_provider_credentials())
        self.write_terraformrc()

        try:
            cmd = [self.terraform, "init", "-upgrade", "-no-color"]
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform init failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def apply(self):
        """terraform apply"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-apply-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        if self.data_location:
            os_env.update(self.update_juju_provider_credentials())

        try:
            cmd = [self.terraform, "apply", "-auto-approve", "-no-color"]
            if self.parallelism is not None:
                cmd.append(f"-parallelism={self.parallelism}")
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform apply failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def destroy(self):
        """terraform destroy"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-destroy-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        if self.data_location:
            os_env.update(self.update_juju_provider_credentials())

        try:
            cmd = [self.terraform, "destroy", "-auto-approve", "-no-color"]
            if self.parallelism is not None:
                cmd.append(f"-parallelism={self.parallelism}")
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform destroy failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def output(self) -> dict:
        """terraform output"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-output-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        if self.data_location:
            os_env.update(self.update_juju_provider_credentials())

        try:
            cmd = [self.terraform, "output", "-json", "-no-color"]
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            stdout = process.stdout
            LOG.debug(f"Command finished. stdout={stdout}, stderr={process.stderr}")
            tf_output = json.loads(stdout)
            output = {}
            for key, value in tf_output.items():
                output[key] = value["value"]
            return output
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform output failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def sync(self) -> None:
        """Sync the running state back to the Terraform state file."""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-sync-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        if self.data_location:
            os_env.update(self.update_juju_provider_credentials())

        try:
            cmd = [self.terraform, "apply", "-refresh-only", "-auto-approve"]
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform sync failed: {e.output}")
            LOG.error(e.stderr)
            raise TerraformException(str(e))


class TerraformInitStep(BaseStep):
    """Initialize Terraform with required providers."""

    def __init__(self, tfhelper: TerraformHelper):
        super().__init__(
            "Initialize Terraform", "Initializing Terraform from provider mirror"
        )
        self.tfhelper = tfhelper

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Initialise Terraform configuration from provider mirror,"""
        try:
            self.tfhelper.init()
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))
