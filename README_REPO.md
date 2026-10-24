# Repository Structure

This repository is organized into four main components:

terraform/ – Provisions and manages the Azure infrastructure.
config/ – Contains the runtime configuration deployed to the monitoring servers.
scripts/ – Deployment and utility scripts executed on target VMs.
.github/workflows/ – GitHub Actions pipelines that validate, deploy, and manage infrastructure changes.

Note: Your local copy may contain additional files that are not part of the original repository, such as custom Grafana dashboard JSON files.

Root Directory
README.md

# Primary documentation covering:

Initial setup
Terraform state storage
Azure OIDC authentication
GitHub configuration
SMTP configuration
Grafana dashboards
Operational procedures
.gitignore

Prevents sensitive and generated files from being committed to Git, including:

Terraform state files
.tfvars files
.terraform/ directories
Temporary deployment artifacts such as run.sh
Infrastructure (terraform/)

Contains all infrastructure-as-code definitions used to provision the monitoring environment.

main.tf

Defines the monitoring VM (r3server-tf), including:

Network interface
Public IP address
Network security group
Firewall rules for:
SSH (22)
Prometheus (9090)
Alertmanager (9093)
Grafana (3000)
VM deployment
Microsoft Entra login extension

# Additional responsibilities:

References existing resource groups and subnets without modifying them
Configures the remote Terraform state backend
Defines deployment variables such as:
VM size
Allowed IP ranges
Administrative password
install_monitoring.sh.tftpl

A first-boot script executed only during initial VM creation.

1.Installs and configures:

Prometheus
Alertmanager
Node Exporter
Grafana

2. The script also:

Creates default configuration files
Creates systemd services
Runs only once during VM deployment

Terraform ignores subsequent changes to this file, so modifying it will not recreate the VM.

3. Runtime Configuration (config/)

This directory becomes the source of truth after the monitoring VM is provisioned.

Prometheus
prometheus/prometheus.yml

4. Prometheus configuration including:

Alertmanager integration
Alert rule file loading
Local scrape targets:
Prometheus
Alertmanager
Node Exporter

5.Remote hosts are not defined here because metrics are received through Alloy remote write.

prometheus/rules/basic-alerts.yml

Contains alert definitions for:

Host unavailable
High CPU utilization
High memory usage
Disk usage thresholds
Missing host reporting
Oracle database unavailable
Oracle metrics missing
Alertmanager
alertmanager/alertmanager.yml

Configures:

SMTP settings
Alert recipients
Alert grouping
Notification timing and repeat intervals

SMTP credentials are read from a file on the VM and are never stored in Git.

Alloy
alloy/r3server.alloy

Configuration for the Alloy agent running on the SAP/Oracle server.

Collects metrics from:

Host/system exporters
SAP exporter (:9680)
Oracle exporter (:9161)

Pushes metrics to Prometheus via remote write.

Grafana
grafana/provisioning/datasources/prometheus.yml

Automatically provisions the Prometheus datasource using a fixed datasource ID to ensure dashboard compatibility.

grafana/provisioning/dashboards/dashboards.yml

Instructs Grafana to:

Load dashboard JSON files from the dashboards directory
Refresh for changes every 30 seconds
grafana/dashboards/observability-overview.json

Sample dashboard containing:

Service availability
Memory utilization
Filesystem usage
Oracle metrics

This dashboard is only an example and can be removed.

Custom Dashboards

Any custom dashboard (for example, sap_observability_nw.json) should be stored in:

config/grafana/dashboards/

Deployment Scripts (scripts/)

Scripts executed on target VMs by deployment pipelines.

deploy_observability.sh

Runs on r3server-tf.

Responsibilities:

Compares proposed configuration against the live environment.
Deploys only modified files.
Validates updated configurations.
Restarts only affected services.
Performs automatic rollback on failure.

Managed services:

Prometheus
Alertmanager
Grafana

Outputs one of:

DEPLOY_RESULT=OK


or

DEPLOY_RESULT=FAIL


for GitHub Actions to process.

deploy_alloy.sh

Runs on r3server.

Responsibilities:

Updates /etc/alloy/config.alloy when changes are detected
Restarts Alloy
Restores the previous configuration if Alloy fails to remain active
prepare_dashboard.sh

Utility script executed locally.

Purpose:

Converts Grafana dashboards downloaded from grafana.com
Replaces datasource references with the repository-standard Prometheus datasource
Saves processed dashboards into:
config/grafana/dashboards/

CI/CD Pipelines (.github/workflows/)

GitHub Actions workflows responsible for validation, deployment, and infrastructure management.

validate.yml

Runs on pull requests and is reused by deployment workflows.

Validation checks include:

Prometheus alert rules using promtool
Alertmanager configuration using amtool
YAML syntax validation
JSON syntax validation
Shell script validation
deploy-observability.yml

Triggers after a merge affecting:

Prometheus
Alertmanager
Grafana

Workflow steps:

Run validations
Wait for manual approval
Authenticate with Azure
Execute deploy_observability.sh on r3server-tf
deploy-alloy.yml

Triggers after Alloy configuration changes are merged.

Workflow steps:

Run validations
Wait for manual approval
Authenticate with Azure
Execute deploy_alloy.sh on r3server
terraform.yml
Pull Request Stage

Runs:

terraform fmt
terraform validate
terraform plan


when changes are made inside terraform/.

Merge Stage

After approval, executes:

terraform apply


to provision or update infrastructure.

How Everything Works Together
1. Initial Deployment

Terraform provisions the monitoring VM and executes the first-boot installation script.

terraform/ → VM Creation → install_monitoring.sh.tftpl

2. Configuration Updates

All ongoing changes are made in the config/ directory.

config/ → Pull Request → validate.yml

3. Deployment

After approval and merge, GitHub Actions deploys the configuration to the appropriate VM.

Merge → Approval → Azure Login → Deployment Workflow

4. Safe Rollout

Deployment scripts running on the VM:

Install only changed files
Validate configurations
Restart only impacted services
Automatically roll back if validation or restart fails
New Config
    ↓
Validation
    ↓
Service Restart
    ↓
Success ✅

or

Failure
    ↓
Automatic Rollback ✅

Lifecycle Summary
Terraform
    ↓
Create Monitoring VM
    ↓
Install Monitoring Stack
    ↓
Manage Configurations in config/
    ↓
Pull Request Validation
    ↓
Approved Merge
    ↓
GitHub Actions Deployment
    ↓
Validation on VM
    ↓
Restart Changed Services
    ↓
Rollback if Needed


This design separates infrastructure, configuration, deployment logic, and automation pipelines, making the monitoring platform predictable, auditable, and safe to update.
