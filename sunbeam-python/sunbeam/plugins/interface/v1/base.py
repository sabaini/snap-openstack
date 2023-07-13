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
from abc import ABC, abstractmethod

import click
from packaging.version import Version

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.jobs.common import read_config, update_config
from sunbeam.plugins.interface import utils

LOG = logging.getLogger(__name__)


class ClickInstantiator:
    """Support invoking click commands on instance methods."""

    def __init__(self, command, klass):
        self.command = command
        self.klass = klass

    def __call__(self, *args, **kwargs):
        return self.command(self.klass(), *args, **kwargs)


class BasePlugin(ABC):
    # Version of plugin interface used by Plugin
    interface_version = Version("0.0.1")

    # Version of plugin
    version = Version("0.0.0")

    def __init__(self, name):
        self.name = name
        self.client = Client()

    @property
    def plugin_key(self) -> str:
        return f"Plugin-{self.name}"

    @classmethod
    def install_hook() -> None:
        pass

    @classmethod
    def upgrade_hook() -> None:
        pass

    @classmethod
    def configure_hook() -> None:
        pass

    @classmethod
    def pre_refresh_hook() -> None:
        pass

    @classmethod
    def post_refresh_hook() -> None:
        pass

    @classmethod
    def remove_hook() -> None:
        pass

    def get_plugin_info(self) -> dict:
        """Get plugin information from clusterdb."""
        try:
            return read_config(self.client, self.plugin_key)
        except ConfigItemNotFoundException as e:
            LOG.debug(str(e))
            return {}

    def update_plugin_info(self, info: dict) -> None:
        """Update plugin information in clusterdb."""
        info_from_db = self.get_plugin_info()
        info_from_db.update(info)
        info_from_db.update({"version": str(self.version)})
        update_config(self.client, self.plugin_key, info_from_db)

    def validate_commands(self) -> bool:
        """validate the commands dictionary.

        Validate if the dictionary follows the format
        {<group>: [{"name": <command name>, "command": <command function>}]}
        """
        for group, commands in self.commands().items():
            for command in commands:
                cmd_name = command.get("name")
                cmd_func = command.get("command")
                if None in (cmd_name, cmd_func):
                    LOG.warning(
                        f"Plugin {self.name}: Commands dictionary is not in "
                        "required format"
                    )
                    return False

                if not any(
                    [
                        isinstance(cmd_func, click.Group),
                        isinstance(cmd_func, click.Command),
                    ]
                ):
                    LOG.warning(
                        f"Plugin {self.name}: {cmd_func} should be either "
                        "click.Group or click.Command"
                    )
                    return False

        return True

    def is_openstack_control_plane(self):
        """Is plugin deploys openstack control plane."""
        return False

    def is_cluster_bootstrapped(self):
        """Is sunbeam cluster bootstrapped."""
        return self.client.cluster.check_sunbeam_bootstrapped()

    @abstractmethod
    def commands(self) -> dict:
        """Dict of clickgroup along with commands.

        Should be of form
        {<group>: [{"name": <command name>, "command": <command function>}]}

        command can be click.Group or click.Command.

        Example:
        {
            "enable": [
                {
                    "name": "subcmd",
                    "command": self.enable_subcmd,
                },
            ],
            "disable": [
                {
                    "name": "subcmd",
                    "command": self.disable_subcmd,
                },
            ],
            "init": [
                {
                    "name": "subgroup",
                    "command": self.trobuleshoot,
                },
            ],
            "subgroup": [
                {
                    "name": "subcmd",
                    "command": self.troubleshoot_subcmd,
                },
            ],
        }

        Based on above example, expected the subclass to define following functions:

        @click.command()
        def enable_subcmd(self):
            pass

        @click.command()
        def disable_subcmd(self):
            pass

        @click.group()
        def troublshoot(self):
            pass

        @click.command()
        def troubleshoot_subcmd(self):
            pass

        Example of one function that requires options:

        @click.command()
        @click.option(
            "-t",
            "--token",
            help="Ubuntu Pro token to use for subscription attachment",
            prompt=True,
        )
        def enable_subcmd(self, token: str):
            pass

        The user can invoke the above commands like:

        sunbeam enable subcmd
        sunbeam disable subcmd
        sunbeam troubleshoot subcmd
        """

    def register(self, cli: click.Group):
        """Register plugin groups and commands."""
        LOG.debug(f"Registering plugin {self.name}")
        if not self.validate_commands():
            LOG.warning(f"Not able to register the plugin {self.name}")
            return

        groups = utils.get_all_registered_groups(cli)
        LOG.debug(f"Registered groups: {groups}")
        for group, commands in self.commands().items():
            group_obj = groups.get(group)
            if not group_obj:
                cmd_names = [command.get("name") for command in commands]
                LOG.warning(
                    f"Plugin {self.name}: Not able to register command "
                    f"{cmd_names} in group {group} as group does not exist"
                )
                continue

            for command in commands:
                cmd = command.get("command")
                cmd_name = command.get("name")
                if cmd_name in group_obj.list_commands({}):
                    if isinstance(cmd, click.Command):
                        LOG.warning(
                            f"Plugin {self.name}: Discarding adding command "
                            f"{cmd_name} as it already exists in group {group}"
                        )
                    else:
                        # Should be sub group and already exists
                        LOG.debug(
                            f"Plugin {self.name}: Group {cmd_name} already "
                            f"part of parent group {group}"
                        )
                    continue

                cmd.callback = ClickInstantiator(cmd.callback, type(self))
                group_obj.add_command(cmd, cmd_name)
                LOG.debug(
                    f"Plugin {self.name}: Command {cmd_name} registered in "
                    f"group {group}"
                )

                # Add newly created click groups to the registered groups so that
                # commands within the plugin can be registered on group.
                # This allows plugin to create new groups and commands in single place.
                if isinstance(cmd, click.Group):
                    groups[cmd_name] = cmd


class EnableDisablePlugin(BasePlugin):
    interface_version = Version("0.0.1")

    def __init__(self, name: str):
        super().__init__(name=name)

    @property
    def enabled(self) -> bool:
        info = self.get_plugin_info(self.plugin_key)
        return info.get("enabled", "false").lower() == "true"

    def pre_enable(self):
        pass

    def post_enable(self):
        pass

    @abstractmethod
    def run_enable_plans(self):
        """Run plans to enable plugin."""

    @abstractmethod
    @click.command()
    def enable_plugin(self):
        self.pre_enable()
        self.run_enable_plans()
        self.post_enable()
        self.update_plugin_info({"enabled": "true"})

    def pre_disable(self):
        pass

    def post_disable(self):
        pass

    @abstractmethod
    def run_disable_plans(self):
        """Run plans to disable plugin."""

    @abstractmethod
    @click.command()
    def disable_plugin(self):
        self.pre_disable()
        self.run_disable_plans()
        self.post_disable()
        self.update_plugin_info({"enabled": "false"})

    def commands(self) -> dict:
        return {
            "enable": [{"name": self.name, "command": self.enable_plugin}],
            "disable": [{"name": self.name, "command": self.disable_plugin}],
        }
