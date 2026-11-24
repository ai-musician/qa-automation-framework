#!/usr/bin/env bash
# Creates a throwaway Kubernetes cluster, runs this repository's Kubernetes tests on it (scripts/k8s_test.sh: API + UI
# tests, chaos, rolling upgrades), and destroys it. Locally with kind, or on AWS (EKS), Google Cloud (GKE) or Azure
# (AKS) with the Terraform in infra/clusters/.
#
#   scripts/cluster.sh <kind|eks|gke|aks> all       # create, test, destroy: destroys even if the tests fail or you
#                                                   # press Ctrl-C; exits non-zero if the tests failed
#   scripts/cluster.sh <kind|eks|gke|aks> plan      # what create would make, without making it (not kind)
#   scripts/cluster.sh <kind|eks|gke|aks> create    # the cluster, and on a cloud a registry for the images
#   scripts/cluster.sh <kind|eks|gke|aks> test      # build and push the images, deploy, run the tests
#   scripts/cluster.sh <kind|eks|gke|aks> destroy   # delete everything create made
#
# Needs Docker, kubectl, and: kind | terraform + aws | terraform + gcloud (+ gke-gcloud-auth-plugin) | terraform + az,
# logged in to that cloud. Settings, all optional:
#   CLUSTER_NAME          default qa (kind) or qa-test (clouds)
#   TF_VAR_admin_cidrs    who may reach the cluster's API, default your current public IP: '["203.0.113.7/32"]'
#   TF_VAR_project_id     GKE project, default your gcloud project
#   TF_VAR_subscription_id AKS subscription, default your az subscription
#   TF_VAR_region | TF_VAR_zone | TF_VAR_location, TF_VAR_node_type, TF_VAR_node_count: see infra/clusters/*/main.tf
#
# The kubeconfig of a cloud cluster goes to .clusters/<cloud>.kubeconfig, never into ~/.kube/config, and the
# Terraform state stays in infra/clusters/<cloud>/ (both git-ignored): run destroy from this same checkout.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 2; }
[ $# -eq 2 ] || usage
provider=$1 action=$2
case $provider in kind|eks|gke|aks) ;; *) usage ;; esac
case $action in plan|create|test|destroy|all) ;; *) usage ;; esac

# gcloud components (gke-gcloud-auth-plugin) live in the SDK's bin, which a Homebrew install leaves off PATH;
# kubectl runs the plugin for every call to a GKE cluster, so it has to be findable
if [ "$provider" = gke ] && command -v gcloud >/dev/null; then
  PATH="$PATH:$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null)/bin"
fi

need() { for c in "$@"; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done; }
case $provider in
  kind) need docker kubectl kind ;;
  eks)  need docker kubectl terraform aws ;;
  gke)  need docker kubectl terraform gcloud gke-gcloud-auth-plugin ;;
  aks)  need docker kubectl terraform az ;;
esac

# --- kind: the local cluster scripts/k8s_test.sh already knows
if [ "$provider" = kind ]; then
  export CLUSTER="${CLUSTER_NAME:-qa}"
  kind_create()  { kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s; }
  kind_test()    { scripts/k8s_test.sh; }
  kind_destroy() { kind delete cluster --name "$CLUSTER"; }
  case $action in
    plan)    echo "kind has nothing to plan: create makes a local cluster named $CLUSTER" ;;
    create)  kind_create ;;
    test)    kind_test ;;
    destroy) kind_destroy ;;
    all)     trap 'status=$?; kind_destroy; exit $status' EXIT; kind_create; kind_test ;;
  esac
  exit 0
fi

# --- clouds: Terraform makes the cluster and registry; the same k8s_test.sh runs against them
tfdir="infra/clusters/$provider"
kubeconfig="$PWD/.clusters/$provider.kubeconfig"
mkdir -p .clusters
tf()  { terraform -chdir="$tfdir" "$@"; }
out() { tf output -raw "$1"; }

export TF_VAR_name="${CLUSTER_NAME:-qa-test}"
export TF_IN_AUTOMATION=1
if [ -z "${TF_VAR_admin_cidrs:-}" ]; then
  ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
  export TF_VAR_admin_cidrs="[\"$ip/32\"]"
fi
case $provider in
  gke) export TF_VAR_project_id="${TF_VAR_project_id:-$(gcloud config get-value project 2>/dev/null)}"
       [ -n "$TF_VAR_project_id" ] || { echo "set TF_VAR_project_id or: gcloud config set project <id>" >&2; exit 1; } ;;
  aks) export TF_VAR_subscription_id="${TF_VAR_subscription_id:-$(az account show --query id -o tsv)}" ;;
esac

cloud_plan() {
  tf init -input=false >/dev/null
  tf plan -input=false
}

cloud_create() {
  echo "--- creating $TF_VAR_name on $provider (API reachable from $TF_VAR_admin_cidrs); this takes 5-15 minutes"
  tf init -input=false >/dev/null
  tf apply -input=false -auto-approve
  echo "--- kubeconfig -> $kubeconfig"
  rm -f "$kubeconfig"
  case $provider in
    eks) aws eks update-kubeconfig --name "$(out cluster_name)" --region "$(out region)" --kubeconfig "$kubeconfig" ;;
    gke) KUBECONFIG="$kubeconfig" gcloud container clusters get-credentials "$(out cluster_name)" \
           --location "$(out location)" --project "$(out project_id)" ;;
    aks) az aks get-credentials --resource-group "$(out resource_group)" --name "$(out cluster_name)" \
           --file "$kubeconfig" --overwrite-existing ;;
  esac
  KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout=300s
  KUBECONFIG="$kubeconfig" kubectl get nodes -o wide
}

cloud_test() {
  [ -f "$kubeconfig" ] || { echo "no cluster: run scripts/cluster.sh $provider create first" >&2; exit 1; }
  local registry; registry=$(out registry)
  echo "--- logging Docker in to $registry"
  case $provider in
    eks) aws ecr get-login-password --region "$(out region)" | docker login --username AWS --password-stdin "${registry%%/*}" ;;
    gke) gcloud auth configure-docker "${registry%%/*}" --quiet ;;
    aks) az acr login --name "$(out registry_name)" ;;
  esac
  KUBECONFIG="$kubeconfig" KUBE_CONTEXT="$(KUBECONFIG="$kubeconfig" kubectl config current-context)" \
    IMAGE_REGISTRY="$registry" scripts/k8s_test.sh
}

cloud_destroy() {
  if [ ! -f "$tfdir/terraform.tfstate" ]; then echo "--- nothing to destroy on $provider"; return 0; fi
  echo "--- destroying $TF_VAR_name on $provider"
  tf init -input=false >/dev/null
  tf destroy -input=false -auto-approve
  rm -f "$kubeconfig" .clusters/gke_gcloud_auth_plugin_cache
  echo "--- destroyed; left in the state: $(tf state list | wc -l | tr -d ' ') resources"
}

case $action in
  plan)    cloud_plan ;;
  create)  cloud_create ;;
  test)    cloud_test ;;
  destroy) cloud_destroy ;;
  all)
    # destroy whatever happens, so a failed test or Ctrl-C never leaves a cluster running up a bill
    trap 'status=$?; trap - EXIT INT TERM; cloud_destroy || echo "!!! destroy failed: run scripts/cluster.sh $provider destroy" >&2; exit $status' EXIT
    trap 'exit 130' INT TERM
    cloud_create
    cloud_test
    ;;
esac
