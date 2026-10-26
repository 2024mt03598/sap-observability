# SAPControl exporter

Prometheus exporter that reads the SAPControl web service (sapstartsrv) of an SAP instance and turns the results into
metrics. Alloy scrapes it on the SAP host and pushes the metrics through the existing remote_write pipeline.

```
sapstartsrv  <--SOAP-->  sapcontrol_exporter (127.0.0.1:9681)  <--scrape--  Alloy  --remote_write-->  Prometheus
```

* Python 3.6+, standard library only (SLES 15 SP5 default `python3` is enough). No pip install.
* **Read-only.** Only methods from a fixed allow-list are called. Start, Stop, RestartInstance, Shutdown, SendSignal,
  OSExecute, SetProcessParameter, ABAPAcknowledgeAlerts, ABAPSetServerInactive, UpdateSystem, EnqRemoveLocks, GWDelete* and
  every other method that changes something are not in the code and cannot be switched on by configuration.
* Fails safely: an unreachable sapstartsrv gives `sapcontrol_up 0`; one failing method only sets its own
  `sapcontrol_method_success` to 0 and records the error text.

## Methods and metrics

| SAPControl method | Metrics (prefix `sapcontrol_`) | Default |
|---|---|---|
| GetProcessList | `process_status` (1 gray, 2 green, 3 yellow, 4 red), `process_uptime_seconds` | on |
| GetSystemInstanceList | `system_instance_info` | on |
| GetInstanceProperties | `instance_attribute_info` | on |
| GetVersionInfo | `version_info` | on |
| GetQueueStatistic | `queue_now`, `queue_high`, `queue_max`, `queue_writes_total`, `queue_reads_total` | on |
| GetAccessPointList | `access_point_active` | on |
| ABAPGetWPTable (SM50) | `abap_workprocesses{type,status}`, `abap_workprocess_running`, `abap_workprocess_errors`, `abap_workprocess_cpu_seconds`, `abap_workprocess_longest_running_seconds` | on |
| ABAPGetSystemWPTable (SM66) | `abap_system_workprocesses{sap_instance,type,status}` | on |
| ABAPReadSyslog (SM21) | `abap_syslog_entries{severity}`, `abap_syslog_entries_by_message` | on |
| ABAPGetComponentList | `abap_component_info` | on |
| ABAPCheckRFCDestinations | `abap_rfc_destinations`, `abap_rfc_destination_info` | on |
| GetSystemUpdateList | `rks_instance_status` | on |
| GetAlertTree (RZ20) | `ccms_nodes{state}`, `ccms_node_status{name,depth}` | on |
| GetAlerts (RZ20) | `ccms_alerts{state}`, `ccms_alert_status` | off (can be large) |
| EnqGetStatistic2 / EnqGetStatistic | `enqueue_*` (every numeric field of the statistic) | on |
| EnqGetLockTable | `enqueue_lock_table_entries`, `..._by_mode` | on |
| ICMGetThreadList / ICMGetConnectionList | `icm_threads`, `icm_thread_requests_total`, `icm_connections` | on |
| ICMGetCacheEntries | `icm_cache_entries`, `icm_cache_bytes` | off |
| GWGetConnectionList / GWGetClientList | `gateway_connections`, `gateway_clients` | on |

Exporter health: `sapcontrol_up`, `sapcontrol_method_success{method}`, `sapcontrol_method_duration_seconds{method}`,
`sapcontrol_method_error_info{method,error}`, `sapcontrol_scrape_duration_seconds`, `sapcontrol_exporter_info`.

Not included: the AS Java (J2EE*) and Web Dispatcher (WebDisp*) methods, because your system is ABAP only, and the
log/trace/profile text readers (ReadLogFile, GetStartProfile, ...), which return text and not numbers. Every group is a small function
in `COLLECTORS`; add a function and a line to the list to extend it.

To keep the number of series bounded, long tables (work processes, syslog, locks, connections) are summarised into counts. Use
`--max-series` and `--ccms-depth` to change the caps.

## Logging in to sapstartsrv

Since release 7.38 nearly all methods are protected by default (`service/protectedwebmethods=SDEFAULT`). Two ways work:

1. **Trusted local Unix socket (default, `SAPCONTROL_SOCKET=auto`).** The exporter runs on the SAP host and uses
   `/tmp/.sapstream5<NR>13` (instance 00: `/tmp/.sapstream50013`). No password is needed. Run the service as root, or as a user that can open the socket.
2. **HTTP with basic authentication.** Set `SAPCONTROL_SOCKET=none`, `SAPCONTROL_USER` and `SAPCONTROL_PASSWORD_FILE`. The OS user must be allowed by
   `service/admin_users`, `service/admin_groups`, or by file permissions on sapstartsrv. Keep the password file at mode 600.

If a method returns `Permission denied` or HTTP 401, that method is protected and the login does not allow it: check
`sapcontrol_method_error_info` in the metrics.

## Install by hand (first test)

```
sudo install -m 0755 sapcontrol_exporter.py /usr/local/bin/
python3 /usr/local/bin/sapcontrol_exporter.py --instance-nr 00 --once | head -40      # prints the metrics once
python3 /usr/local/bin/sapcontrol_exporter.py --instance-nr 00 --list-methods
```

Then install the service:

```
sudo cp sapcontrol-exporter.service /etc/systemd/system/
sudo cp sapcontrol-exporter.env.example /etc/sapcontrol-exporter.env && sudo chmod 600 /etc/sapcontrol-exporter.env   # edit it
sudo systemctl daemon-reload && sudo systemctl enable --now sapcontrol-exporter
curl -s localhost:9681/metrics | grep -E '^sapcontrol_(up|method_success)'
```

## Install through the pipeline

Merging a change under `exporters/sapcontrol/` runs `deploy-sapcontrol-exporter.yml`: self-test on the runner, approval, then install on r3server, restart
only if something changed, check that `/metrics` answers, and roll back otherwise. The environment file `/etc/sapcontrol-exporter.env` stays on the server
(it can hold a user name) and is created once by hand.

Order matters for the first rollout: deploy the exporter first, then merge the Alloy change that scrapes port 9681, otherwise `up` for that target is 0.

## Test without SAP

`python3 test_exporter.py` starts `mock_sapstartsrv.py` (canned answers in the documented structures) and checks 25 conditions, including
that a protected method only fails itself and that mutating methods are refused. It proves the parsing and metric output. It does **not** prove that a real
sapstartsrv answers the same way.

## Things to verify on the real system (not proven yet)

1. The SOAP namespace `urn:SAPControl`. Check `http://localhost:50013/?wsdl` (`targetNamespace`). Change with `--namespace` if different.
2. Field formats such as work-process `Cpu` and `Time`. Unparsable values are skipped without errors.
3. Which methods your kernel and instance support (for example enqueue methods only exist on the instance that runs the enqueue server). Unsupported ones show up as
   `sapcontrol_method_success 0`; remove them with `SAPCONTROL_SKIP`.
4. The CCMS tree parent index (assumed to start at 0). The first lines of `--once` output will show if `ccms_node_status` looks sensible.
