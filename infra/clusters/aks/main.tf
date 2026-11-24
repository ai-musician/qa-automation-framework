# A throwaway AKS cluster for this repository's Kubernetes tests: scripts/cluster.sh aks create|test|destroy.
#
# Everything is in one resource group, so destroy removes it all: the cluster (free tier control plane, two small
# nodes) and a container registry that only the cluster's nodes may pull from. The Kubernetes API answers only the
# addresses in admin_cidrs (scripts/cluster.sh passes yours) and the cluster's own outbound address.
# Roughly $0.20 an hour (two Standard_D2s_v5 + Basic registry).

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "subscription_id" {
  type = string
}

variable "name" {
  type    = string
  default = "qa-test"
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "node_type" {
  type    = string
  default = "Standard_D2s_v5"
}

variable "node_count" {
  type    = number
  default = 2
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Addresses allowed to reach the Kubernetes API, e.g. [\"203.0.113.7/32\"]"
}

locals {
  tags = {
    project    = "qa-automation"
    purpose    = "test-cluster"
    cluster    = var.name
    managed-by = "terraform"
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

resource "azurerm_resource_group" "this" {
  name     = "${var.name}-rg"
  location = var.location
  tags     = local.tags
}

resource "random_string" "registry" { # registry names are global: letters and digits only
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_container_registry" "images" {
  name                = "${replace(var.name, "-", "")}${random_string.registry.result}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic"
  admin_enabled       = false # pushes use your Azure login, pulls the nodes' identity
  tags                = local.tags
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.name
  sku_tier            = "Free"
  tags                = local.tags

  default_node_pool {
    name       = "default"
    node_count = var.node_count
    vm_size    = var.node_type
  }

  identity {
    type = "SystemAssigned"
  }

  api_server_access_profile {
    authorized_ip_ranges = var.admin_cidrs
  }
}

resource "azurerm_role_assignment" "nodes_pull" {
  scope                            = azurerm_container_registry.images.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "registry" {
  value = azurerm_container_registry.images.login_server
}

output "registry_name" {
  value = azurerm_container_registry.images.name
}
