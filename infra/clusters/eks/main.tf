# A throwaway EKS cluster for this repository's Kubernetes tests: scripts/cluster.sh eks create|test|destroy.
#
# Two small nodes in public subnets: no NAT gateway to pay for, and the nodes' security group still lets nothing in
# from the internet. The Kubernetes API answers only the addresses in admin_cidrs (scripts/cluster.sh passes yours)
# and, privately, the nodes. Two ECR repositories hold the images; destroy deletes them with their images.
# Roughly $0.27 an hour while it exists (control plane $0.10 + two t3.large).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "name" {
  type    = string
  default = "qa-test"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "kubernetes_version" {
  type        = string
  default     = "1.37" # EKS default and in standard support until 2027-11; must be set (null breaks the node group's plan)
  description = "aws eks describe-cluster-versions --default-only shows the current default"
}

variable "node_type" {
  type    = string
  default = "t3.large"
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

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name                    = var.name
  cidr                    = "10.0.0.0/16"
  azs                     = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets          = ["10.0.0.0/20", "10.0.16.0/20"]
  map_public_ip_on_launch = true
  enable_nat_gateway      = false
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name                                     = var.name
  kubernetes_version                       = var.kubernetes_version
  endpoint_public_access                   = true
  endpoint_public_access_cidrs             = var.admin_cidrs
  endpoint_private_access                  = true # the test pods call the Kubernetes API (chaos, upgrade tests)
  enable_cluster_creator_admin_permissions = true # whoever runs create can use kubectl

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  addons = {
    vpc-cni = {
      before_compute = true
    }
    kube-proxy = {}
    coredns    = {}
  }

  eks_managed_node_groups = {
    default = {
      instance_types = [var.node_type]
      min_size       = var.node_count
      max_size       = var.node_count
      desired_size   = var.node_count
    }
  }
}

resource "aws_ecr_repository" "images" {
  for_each     = toset(["qa-api", "qa-tests"])
  name         = "${var.name}/${each.key}"
  force_delete = true # destroy also deletes the pushed images
  image_scanning_configuration {
    scan_on_push = true
  }
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "registry" {
  value = trimsuffix(aws_ecr_repository.images["qa-api"].repository_url, "/qa-api")
}
