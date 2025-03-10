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

terraform {

  required_providers {
    juju = {
      source  = "juju/juju"
      version = "= 0.10.1"
    }
  }

}

provider "juju" {}

data "juju_model" "controller" {
  name = "controller"
}

resource "juju_application" "microceph" {
  name  = "microceph"
  trust = true
  model = data.juju_model.controller.name
  units = length(var.machine_ids) # need to manage the number of units

  charm {
    name     = "microceph"
    channel  = var.charm_microceph_channel
    revision = var.charm_microceph_revision
    base    = "ubuntu@22.04"
  }

  config = merge({
    snap-channel = var.microceph_channel
  }, var.charm_microceph_config)
}

# juju_offer.microceph_offer will be created
resource "juju_offer" "microceph_offer" {
  application_name = juju_application.microceph.name
  endpoint         = "ceph"
  model            = data.juju_model.controller.name
}
