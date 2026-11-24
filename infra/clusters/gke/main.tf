# A throwaway GKE cluster for this repository's Kubernetes tests: scripts/cluster.sh gke create|test|destroy.
#
# A zonal cluster (its control plane is covered by GKE's free tier, one per billing account) with two small nodes
# that run as their own service account, which may only pull from this cluster's Artifact Registry repository and
# write logs and metrics. The Kubernetes API answers only the addresses in admin_cidrs (scripts/cluster.sh passes
# yours). Destroy deletes the repository with its images. Roughly $0.14 an hour (two e2-standard-2).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

variable "project_id" {
  type = string
}

variable "name" {
  type    = string
  default = "qa-test"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "network" {
  type    = string
  default = "default"
}

variable "node_type" {
  type    = string
  default = "e2-standard-2"
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
  region = join("-", slice(split("-", var.zone), 0, 2)) # us-central1-a -> us-central1
}

provider "google" {
  project = var.project_id
  region  = local.region
  default_labels = {
    project    = "qa-automation"
    purpose    = "test-cluster"
    cluster    = var.name
    managed-by = "terraform"
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(["container.googleapis.com", "artifactregistry.googleapis.com"])
  service            = each.key
  disable_on_destroy = false # other things in the project may use them
}

resource "google_artifact_registry_repository" "images" {
  location      = local.region
  repository_id = var.name
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

resource "google_service_account" "nodes" {
  account_id   = "${var.name}-nodes"
  display_name = "Nodes of the ${var.name} test cluster"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_artifact_registry_repository_iam_member" "nodes_pull" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "this" {
  name                     = var.name
  location                 = var.zone
  network                  = var.network
  deletion_protection      = false # a test cluster: destroy must be able to delete it
  remove_default_node_pool = true  # replaced by the node pool below
  initial_node_count       = 1

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.admin_cidrs
      content {
        cidr_block = cidr_blocks.value
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_container_node_pool" "default" {
  name       = "default"
  cluster    = google_container_cluster.this.id
  location   = var.zone
  node_count = var.node_count

  node_config {
    machine_type    = var.node_type
    disk_type       = "pd-standard"
    disk_size_gb    = 50
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"] # what it may do is set by IAM above
  }
}

output "cluster_name" {
  value = google_container_cluster.this.name
}

output "location" {
  value = var.zone
}

output "project_id" {
  value = var.project_id
}

output "registry" {
  value = "${local.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
