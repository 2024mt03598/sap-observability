## Startup  Sequence  (SAP , DB , Exporters)

Start SAP systems : 

# 1 > start the Database  

Login in with user orasre on r3server & execute commands below on sql prompt 

#r3server > sudo su  - orasre 
#r3server > sqlplus / as sysdba 
#r3server >  startup 

# 2 >  Start  oracle Listener 

Login in with user orasre on r3server & execute commands below  

#r3server > sudo su  - orasre 
#r3server > lsnrctl  start LISTENER 

 
# 3 >  Start SAP systems. 

Login in with user sreadm on r3server & execute commands below  

#r3server > sudo su  - sreadm  
#r3server >  startsap 
 

 ## Start Data collectors 

 
# 1. Start SAP Host Exporter 

systemctl start prometheus-sap_host_exporter@D00 

systemctl status prometheus-sap_host_exporter@D00 

  

# 2. Start Oracle DB Exporter 

sudo systemctl start oracle-db-exporter 

sudo systemctl status oracle-db-exporter 

  

# 3. Start Alloy 

sudo systemctl start alloy --no-pager 

sudo systemctl status alloy --no-pager 

Stop Sequence 

Stop the components in the reverse order: 

Shell 

# 1. Stop Alloy 

sudo systemctl stop alloy --no-pager 
sudo systemctl status alloy --no-pager 

# 2. Stop Oracle DB Exporter 

sudo systemctl stop oracle-db-exporter 
sudo systemctl status oracle-db-exporter 

# 3. Stop SAP Host Exporter 

systemctl stop prometheus-sap_host_exporter@D00 
systemctl status prometheus-sap_host_exporter@D00 

Summary 



## Stop SAP systems : 

  
# 1>  Stop SAP systems. 

Login in with user sreadm on r3server & execute commands below  
#r3server > sudo su  - sreadm  
#r3server >  stopsap r3 


# 2 >  Stop oracle Listener 

Login in with user orasre on r3server & execute commands below  
#r3server > sudo su  - orasre 
#r3server > lsnrctl  stop LISTENER 


# 3> Stop the Database  

Login in with user orasre on r3server & execute commands below on sql prompt 
#r3server > sudo su  - orasre 
#r3server > sqlplus / as sysdba 
 >  shutdown immediate
 

# Status Check : 

r3server:~ #  
r3server:~ # sudo systemctl status alloy --no-pager  
● alloy.service - Vendor-agnostic OpenTelemetry Collector distribution with programmable pipelines 

     Loaded: loaded (/usr/lib/systemd/system/alloy.service; enabled; vendor preset: disabled) 

     Active: active (running) since Tue 2026-10-20 04:09:15 UTC; 1h 37min ago 

       Docs: https://grafana.com/docs/alloy 

   Main PID: 1852 (alloy) 

      Tasks: 17 

        CPU: 16.114s 

     CGroup: /system.slice/alloy.service 

             └─ 1852 /usr/bin/alloy run --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy 


r3server:~ # sudo systemctl status oracle-db-exporter  
● oracle-db-exporter.service - Oracle DB Exporter for Prometheus 

     Loaded: loaded (/etc/systemd/system/oracle-db-exporter.service; enabled; vendor preset: disabled) 

     Active: active (running) since Tue 2026-10-20 04:09:14 UTC; 1h 38min ago 

   Main PID: 1623 (oracledb_export) 

      Tasks: 17 

        CPU: 814ms 

     CGroup: /system.slice/oracle-db-exporter.service 

             └─ 1623 /usr/local/bin/oracledb_exporter --config.file=/etc/oracle-db-exporter/config.yaml 


r3server:~ # systemctl status prometheus-sap_host_exporter@D00  

● prometheus-sap_host_exporter@D00.service - Prometheus exporter for Netweaver clusters metrics 

     Loaded: loaded (/usr/lib/systemd/system/prometheus-sap_host_exporter@.service; disabled; vendor preset: disabled) 

     Active: active (running) since Tue 2026-10-20 05:28:37 UTC; 18min ago 

       Docs: https://github.com/SUSE/sap_host_exporter 

   Main PID: 22457 (sap_host_export) 

      Tasks: 4 

        CPU: 55ms 

     CGroup: /system.slice/system-prometheus\x2dsap_host_exporter.slice/prometheus-sap_host_exporter@D00.service 

             └─ 22457 /usr/bin/sap_host_exporter --config /etc/sap_host_exporter/D00.yaml 

# Observability repo (Prometheus, Alertmanager, Grafana, Alloy)

Everything here is deployed by GitHub Actions. Nobody edits files on the servers by hand.

| Change this | Pipeline does this |
|---|---|
| `config/prometheus/**` | validates, copies to r3server-tf, restarts **prometheus** only |
| `config/alertmanager/**` | validates, copies to r3server-tf, restarts **alertmanager** only |
| `config/grafana/provisioning/**` | copies to r3server-tf, restarts **grafana-server** only |
| `config/grafana/dashboards/*.json` | copies to r3server-tf; Grafana loads them within ~30 s, **no restart** |
| `config/alloy/r3server.alloy` | copies to r3server as `/etc/alloy/config.alloy`, restarts **alloy** |
| `terraform/**` | PR: `plan`. Merge to main: `apply` (after approval) |

Each deploy backs up the old config under `/var/backups/config-deploy/` on the VM. If validation fails or the
service does not come back up, it rolls back automatically and the workflow turns red. Unchanged
components are skipped, so no restart happens for them.

The pipeline reaches the VMs with `az vm run-command` (Azure agent), so **no SSH port or inbound firewall rule is needed**.

## Layout
```
terraform/            main.tf, first-boot install script (bootstrap only)
config/prometheus/    prometheus.yml, rules/*.yml
config/alertmanager/  alertmanager.yml
config/grafana/       provisioning/datasources/prometheus.yml
config/alloy/         r3server.alloy   (one file per monitored host)
scripts/              run on the VMs by the pipeline
.github/workflows/    validate, deploy-observability, deploy-alloy, terraform
```
After the first boot, `config/` is the source of truth. The initial config written by the Terraform install
script is only a starting point and is overwritten by the first pipeline run.

## One-time setup

### 1. Remote Terraform state (so the pipeline and your laptop share state)
```
az storage account create -g datapipeline_group -n <UNIQUE_NAME> -l southeastasia --sku Standard_LRS --allow-blob-public-access false --min-tls-version TLS1_2
az storage container create --account-name <UNIQUE_NAME> -n tfstate --auth-mode login
SCOPE=$(az storage account show -g datapipeline_group -n <UNIQUE_NAME> --query id -o tsv)
az role assignment create --assignee <APP_CLIENT_ID> --role "Storage Blob Data Contributor" --scope $SCOPE
az role assignment create --assignee $(az ad signed-in-user show --query id -o tsv) --role "Storage Blob Data Contributor" --scope $SCOPE
```
`<UNIQUE_NAME>`: 3-24 lowercase letters/digits, globally unique. `<APP_CLIENT_ID>`: the app registration you made earlier.

### 2. Move your existing local state into it (do this BEFORE the first pipeline run)
Copy the `terraform.tfstate` from the folder where you ran `terraform apply` into `terraform/`, then:
```
cd terraform
terraform init -migrate-state -backend-config="storage_account_name=<UNIQUE_NAME>"
terraform plan -var "admin_password=..." -var "allowed_source_cidr=..."
```
Answer `yes` to the migration. The plan must say **No changes**. If it wants to create or replace
anything, stop and fix that first. Otherwise the pipeline would try to create a second copy of the VM.

### 3. Let GitHub log in to Azure without a stored secret (OIDC)
Azure portal > Microsoft Entra ID > App registrations > your app > **Certificates & secrets** > **Federated credentials** > **Add credential** > scenario **GitHub Actions deploying Azure resources**. Create two:
1. Entity **Environment**, environment name `production`.
2. Entity **Pull request**.

Organization and repository: your GitHub org/user and repo name. The app already has Contributor on `datapipeline_group`;
that role also allows `az vm run-command` on both VMs, because both are in that resource group.

### 4. GitHub repo settings
*Settings > Secrets and variables > Actions*

Variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` (`7cbf90cf-b4f2-4c84-87df-bdf7797486b5`), `AZURE_SUBSCRIPTION_ID` (`cedc5e01-c693-4b4e-8ccb-ce00f973a598`), `ALLOWED_SOURCE_CIDR`, `TFSTATE_STORAGE_ACCOUNT`

Secret: `TF_VAR_ADMIN_PASSWORD` (the VM's r3user password)

*Settings > Environments > New environment* named `production`: tick **Required reviewers** and add yourself. Every deploy
and Terraform apply then waits for your approval.

*Settings > Branches*: protect `main` (require a pull request and the `validate` check).

### 5. First run
Before the first deploy, remember the pipeline overwrites the live files with the repo version. If you changed anything
by hand on the VMs, copy those changes into `config/` first (for example `cat /etc/prometheus/prometheus.yml` on r3server-tf).
Then go to *Actions > deploy-observability > Run workflow*, approve it, and repeat for *deploy-alloy*.

## Day-to-day
1. Branch, edit a file under `config/`, open a pull request. `validate` runs `promtool`/`amtool`.
2. Merge. The matching deploy workflow starts, waits for approval, then deploys and restarts only what changed.
3. Check the workflow log: it prints per-component results, service status and `DEPLOY_RESULT=OK`.

To add a host: copy `config/alloy/r3server.alloy` to `config/alloy/<host>.alloy`, add a workflow (copy `deploy-alloy.yml`, change `VM_NAME`
and the file name), and the instance label fills in from the hostname.

## Notes
- **Secrets never go in this repo.** The Oracle password is not in any file here. The Alertmanager SMTP password lives in `/etc/alertmanager-secrets/smtp_password` on r3server-tf (see "Email alerts" below). It is outside `/etc/alertmanager` on purpose, because each deploy replaces that folder.
- `terraform/main.tf` ignores changes to `custom_data`, so editing the first-boot script never replaces the VM. To rebuild: `terraform apply -replace=azurerm_linux_virtual_machine.vm`.
- A restart of Prometheus takes a few seconds; Alloy buffers data and resends it, so short gaps normally fill in.
- Run Command needs the Azure VM agent (`waagent`) healthy on the VM. If a run times out, check `systemctl status waagent`.
- Manual rollback: copy the newest folder under `/var/backups/config-deploy/` back to the config directory and restart the service.

## Email alerts (SMTP)
Alertmanager sends mail **outbound** through an SMTP relay, so no inbound port is needed on r3server-tf. Use port **587** (STARTTLS).
Azure blocks outbound port 25 for most subscriptions, and an NSG rule cannot open it.

1. Create the password file on r3server-tf **before** deploying the new `alertmanager.yml`:
   ```
   sudo mkdir -p /etc/alertmanager-secrets
   sudo vi /etc/alertmanager-secrets/smtp_password        # paste ONLY the password / app password, no trailing spaces
   sudo chown -R alertmanager:alertmanager /etc/alertmanager-secrets
   sudo chmod 700 /etc/alertmanager-secrets && sudo chmod 600 /etc/alertmanager-secrets/smtp_password
   ```
2. Edit the three `smtp_*` values and the `to:` address in `config/alertmanager/alertmanager.yml`, open a pull request, merge, approve the deploy.
3. Test: `amtool alert add TestAlert severity=warning --annotation=summary="SMTP test" --alertmanager.url=http://localhost:9093`
   The mail arrives after about 30 seconds (`group_wait`).

## Dashboards
Dashboards are JSON files in `config/grafana/dashboards/`. Grafana loads them automatically (provider in
`config/grafana/provisioning/dashboards/`). They are read-only in the UI: change the JSON in Git, not in Grafana.

**Add a community dashboard (for example Node Exporter Full, ID 1860)**
1. On grafana.com open the dashboard page and click **Download JSON**.
2. Run `scripts/prepare_dashboard.sh ~/Downloads/<file>.json node-exporter-full`. It points the panels at the
   datasource uid `prometheus` (set in `config/grafana/provisioning/datasources/prometheus.yml`) and writes the
   result to `config/grafana/dashboards/node-exporter-full.json`.
3. Commit, open a pull request (the `validate` job checks the JSON), merge, approve the deploy.

**Change a dashboard you built in the Grafana UI**: dashboard > Share > Export > **Export for sharing externally** >
Save to file, then run it through the same script.

Dashboards that are not wired to the `prometheus` datasource show "No data". The datasource is deleted and recreated
on each Grafana restart so its uid is always `prometheus`.
