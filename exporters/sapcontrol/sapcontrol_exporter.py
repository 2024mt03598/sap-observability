#!/usr/bin/env python3
"""
sapcontrol_exporter - Prometheus exporter for the SAPControl web service (sapstartsrv).

* Read-only: only methods from a fixed allow-list are ever called (no Start/Stop/OSExecute/...).
* No third-party packages: Python 3.6+ standard library only.
* Talks SOAP (doc/literal) to sapstartsrv over a trusted local Unix socket or HTTP(S).
* Every method is optional; a failing method only affects its own metrics.

Run once and print the metrics:  python3 sapcontrol_exporter.py --instance-nr 00 --once
"""
import argparse
import base64
import http.client
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from xml.sax.saxutils import escape

VERSION = "0.1.1"
LOG = logging.getLogger("sapcontrol_exporter")
COLORS = {"GRAY": 1, "GREY": 1, "GREEN": 2, "YELLOW": 3, "RED": 4}


# --------------------------------------------------------------------------- helpers
def local(tag):
    return tag.rsplit("}", 1)[-1]


def to_py(el):
    """XML element -> str | dict | list. <item> children become a list."""
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    if all(local(k.tag) == "item" for k in kids):
        return [to_py(k) for k in kids]
    return dict((local(k.tag), to_py(k)) for k in kids)


def as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    return [v]


def rows(resp, key):
    """Return the list of records of a response, tolerant to naming differences."""
    if isinstance(resp, dict):
        if key in resp:
            return as_list(resp[key])
        if len(resp) == 1:
            return as_list(next(iter(resp.values())))
        return []
    return as_list(resp)


def num(x):
    try:
        return float(str(x).strip())
    except (ValueError, TypeError):
        return None


def color(x):
    """'SAPControl-GREEN' | 'GREEN' | 2 -> 1..4 (GRAY, GREEN, YELLOW, RED)."""
    s = str(x).strip()
    if s.isdigit():
        return int(s)
    return COLORS.get(re.split(r"[-_]", s)[-1].upper())


def duration(x):
    """'40:07:48' -> seconds. Returns None when it cannot be parsed."""
    s = str(x).strip()
    if not s:
        return None
    if re.match(r"^\d+(\.\d+)?$", s):
        return float(s)
    parts = s.split(":")
    if all(p.isdigit() for p in parts) and 1 < len(parts) <= 4:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return float(total)
    return None


def metric_name(s):
    return re.sub(r"[^a-zA-Z0-9_]", "_", s).lower()


def short(s, n=120):
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------- metrics
class Registry(object):
    def __init__(self):
        self.meta = {}
        self.order = []
        self.samples = {}

    def add(self, name, value, labels=None, help_="", mtype="gauge"):
        if value is None:
            return
        if name not in self.meta:
            self.meta[name] = (help_, mtype)
            self.order.append(name)
            self.samples[name] = []
        self.samples[name].append((labels or {}, value))

    @staticmethod
    def _esc(v):
        return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def render(self):
        out = []
        for name in self.order:
            help_, mtype = self.meta[name]
            samples = self.samples[name]
            seen = {}
            for labels, _ in samples:
                key = tuple(sorted(labels.items()))
                seen[key] = seen.get(key, 0) + 1
            nth = {}
            out.append("# HELP %s %s" % (name, help_ or name))
            out.append("# TYPE %s %s" % (name, mtype))
            for labels, value in samples:
                key = tuple(sorted(labels.items()))
                if seen[key] > 1:
                    # Identical label sets would make Prometheus drop samples: number them instead.
                    nth[key] = nth.get(key, 0) + 1
                    labels = dict(labels, index=str(nth[key]))
                if labels:
                    lab = ",".join('%s="%s"' % (k, self._esc(v)) for k, v in sorted(labels.items()))
                    out.append("%s{%s} %s" % (name, lab, repr(float(value))))
                else:
                    out.append("%s %s" % (name, repr(float(value))))
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- SOAP client
class SoapError(Exception):
    pass


class Unsupported(SoapError):
    """The instance does not offer this method (for example enqueue methods on a dialog instance)."""


class ConnectError(Exception):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        http.client.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


class SapControl(object):
    def __init__(self, args):
        self.nr = args.instance_nr
        self.port = args.port or int("5%s13" % self.nr if not args.https else "5%s14" % self.nr)
        self.host = args.host
        self.https = args.https
        self.insecure = args.insecure
        self.timeout = args.timeout
        self.ns = args.namespace
        self.user = args.user
        self.password = args.password
        sock = args.socket
        default_sock = "/tmp/.sapstream5%s13" % self.nr
        if sock == "auto":
            sock = default_sock if os.path.exists(default_sock) else None
        elif sock in ("none", "", None):
            sock = None
        self.socket_path = sock

    def describe(self):
        if self.socket_path:
            return "unix socket %s" % self.socket_path
        return "%s://%s:%s" % ("https" if self.https else "http", self.host, self.port)

    def _connect(self):
        if self.socket_path:
            return UnixHTTPConnection(self.socket_path, self.timeout)
        if self.https:
            ctx = ssl.create_default_context()
            if self.insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def call(self, method, params=None):
        inner = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in (params or {}).items())
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:SAPControl="%s"><SOAP-ENV:Body><SAPControl:%s>%s</SAPControl:%s>'
            "</SOAP-ENV:Body></SOAP-ENV:Envelope>" % (self.ns, method, inner, method)
        )
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'}
        if self.user and not self.socket_path:
            token = base64.b64encode(("%s:%s" % (self.user, self.password or "")).encode("utf-8"))
            headers["Authorization"] = "Basic " + token.decode("ascii")
        conn = self._connect()
        try:
            try:
                conn.request("POST", "/", body=body.encode("utf-8"), headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                raise ConnectError("%s: %s" % (self.describe(), exc))
        finally:
            conn.close()

        if resp.status == 401:
            raise SoapError("HTTP 401 Unauthorized: this method needs credentials or a trusted local connection")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise SoapError("HTTP %d with a response that is not XML" % resp.status)
        body_el = None
        for child in root:
            if local(child.tag) == "Body":
                body_el = child
        if body_el is None or not len(body_el):
            raise SoapError("HTTP %d with an empty SOAP body" % resp.status)
        first = body_el[0]
        if local(first.tag) == "Fault":
            text = ""
            for f in first:
                if local(f.tag) == "faultstring":
                    text = (f.text or "").strip()
            if "not supported by this instance type" in text:
                raise Unsupported(text)
            raise SoapError(text or "SOAP fault")
        if resp.status != 200:
            raise SoapError("HTTP %d" % resp.status)
        return to_py(first)


# --------------------------------------------------------------------------- method collectors
# Each collector: fn(ctx, reg) where ctx has .c (client), .base (labels) and .args.
class Ctx(object):
    def __init__(self, client, base, args):
        self.c = client
        self.base = base
        self.args = args

    def lab(self, **kw):
        d = dict(self.base)
        d.update(kw)
        return d


def m_GetProcessList(x, reg):
    for p in rows(x.c.call("GetProcessList"), "process"):
        lbl = x.lab(name=p.get("name", ""), description=p.get("description", ""), pid=p.get("pid", ""))
        reg.add("sapcontrol_process_status", color(p.get("dispstatus")),
                dict(lbl, textstatus=p.get("textstatus", "")),
                "Process state from sapstartsrv: 1=gray 2=green 3=yellow 4=red")
        reg.add("sapcontrol_process_uptime_seconds", duration(p.get("elapsedtime")), lbl,
                "Elapsed time since the process started")


def m_GetSystemInstanceList(x, reg):
    for i in rows(x.c.call("GetSystemInstanceList"), "instance"):
        reg.add("sapcontrol_system_instance_info", 1,
                x.lab(hostname=i.get("hostname", ""), sap_instance_nr=i.get("instanceNr", ""),
                      features=i.get("features", ""), start_priority=i.get("startPriority", ""),
                      http_port=i.get("httpPort", ""), https_port=i.get("httpsPort", "")),
                "Instances of the SAP system (value is always 1)")


def m_GetInstanceProperties(x, reg):
    n = 0
    for p in rows(x.c.call("GetInstanceProperties"), "properties"):
        if p.get("propertytype") == "Attribute" and n < x.args.max_series:
            n += 1
            reg.add("sapcontrol_instance_attribute_info", 1,
                    x.lab(property=p.get("property", ""), value=short(p.get("value", ""))),
                    "Instance attributes (value is always 1)")


def m_GetVersionInfo(x, reg):
    for v in rows(x.c.call("GetVersionInfo"), "version")[: x.args.max_series]:
        reg.add("sapcontrol_version_info", 1,
                x.lab(filename=v.get("Filename", ""), version=short(v.get("VersionInfo", ""))),
                "Version of the most important instance files (value is always 1)")


def m_GetQueueStatistic(x, reg):
    for q in rows(x.c.call("GetQueueStatistic"), "queue"):
        lbl = x.lab(type=q.get("Typ", ""))
        reg.add("sapcontrol_queue_now", num(q.get("Now")), lbl, "Requests currently in the queue")
        reg.add("sapcontrol_queue_high", num(q.get("High")), lbl, "Peak queue length")
        reg.add("sapcontrol_queue_max", num(q.get("Max")), lbl, "Configured maximum queue length")
        reg.add("sapcontrol_queue_writes_total", num(q.get("Writes")), lbl, "Requests written to the queue", "counter")
        reg.add("sapcontrol_queue_reads_total", num(q.get("Reads")), lbl, "Requests read from the queue", "counter")


def m_GetAccessPointList(x, reg):
    for a in rows(x.c.call("GetAccessPointList"), "accesspoint"):
        active = str(a.get("active", "")).strip().upper() in ("YES", "TRUE", "1")
        reg.add("sapcontrol_access_point_active", 1 if active else 0,
                x.lab(address=a.get("address", ""), port=a.get("port", ""),
                      protocol=a.get("protocol", ""), processname=a.get("processname", "")),
                "Network access point of the instance: 1=active")


def m_ABAPGetWPTable(x, reg):
    counts, longest = {}, {}
    for w in rows(x.c.call("ABAPGetWPTable"), "workprocess"):
        typ, status = w.get("Typ", ""), w.get("Status", "")
        counts[(typ, status)] = counts.get((typ, status), 0) + 1
        running = status.lower() in ("running", "run")
        lbl = x.lab(no=w.get("No", ""), type=typ)
        reg.add("sapcontrol_abap_workprocess_running", 1 if running else 0, lbl,
                "ABAP work process is running (SM50): 1=running 0=other")
        reg.add("sapcontrol_abap_workprocess_errors", num(w.get("Err")), lbl,
                "Restarts after errors of the work process")
        reg.add("sapcontrol_abap_workprocess_cpu_seconds", duration(w.get("Cpu")), lbl,
                "CPU time used by the work process")
        if running:
            t = num(w.get("Time"))
            if t is not None:
                longest[typ] = max(longest.get(typ, 0), t)
    for (typ, status), n in sorted(counts.items()):
        reg.add("sapcontrol_abap_workprocesses", n, x.lab(type=typ, status=status),
                "ABAP work processes by type and status")
    for typ, t in sorted(longest.items()):
        reg.add("sapcontrol_abap_workprocess_longest_running_seconds", t, x.lab(type=typ),
                "Longest runtime of a running work process of this type")


def m_ABAPGetSystemWPTable(x, reg):
    counts = {}
    for w in rows(x.c.call("ABAPGetSystemWPTable", {"activeonly": "false"}), "workprocess"):
        k = (w.get("Instance", ""), w.get("Typ", ""), w.get("Status", ""))
        counts[k] = counts.get(k, 0) + 1
    for (inst, typ, status), n in sorted(counts.items()):
        reg.add("sapcontrol_abap_system_workprocesses", n, x.lab(sap_instance=inst, type=typ, status=status),
                "ABAP work processes of all instances (SM66) by type and status")


def m_ABAPReadSyslog(x, reg):
    by_sev, by_msg = {}, {}
    for e in rows(x.c.call("ABAPReadSyslog"), "log"):
        sev = {1: "gray", 2: "green", 3: "yellow", 4: "red"}.get(color(e.get("Severity")), "unknown")
        by_sev[sev] = by_sev.get(sev, 0) + 1
        if sev in ("red", "yellow"):
            k = (sev, e.get("MNo", ""))
            by_msg[k] = by_msg.get(k, 0) + 1
    for sev in ("gray", "green", "yellow", "red"):
        reg.add("sapcontrol_abap_syslog_entries", by_sev.get(sev, 0), x.lab(severity=sev),
                "Entries currently in the ABAP system log (SM21) by severity")
    top = sorted(by_msg.items(), key=lambda kv: -kv[1])[: x.args.max_series]
    for (sev, mno), n in top:
        reg.add("sapcontrol_abap_syslog_entries_by_message", n, x.lab(severity=sev, message_no=mno),
                "Yellow/red syslog entries by message number (top entries only)")


def m_ABAPGetComponentList(x, reg):
    for c in rows(x.c.call("ABAPGetComponentList"), "component")[: x.args.max_series * 4]:
        reg.add("sapcontrol_abap_component_info", 1,
                x.lab(component=c.get("component", ""), release=c.get("release", ""),
                      patchlevel=c.get("patchlevel", ""), componenttype=c.get("componenttype", "")),
                "Installed ABAP software components, CVERS (value is always 1)")


def m_ABAPCheckRFCDestinations(x, reg):
    dests = [d for d in rows(x.c.call("ABAPCheckRFCDestinations"), "destination") if isinstance(d, str)]
    reg.add("sapcontrol_abap_rfc_destinations", len(dests), x.lab(),
            "System-internal RFC destinations that are a single point of failure")
    for d in dests[: x.args.max_series]:
        reg.add("sapcontrol_abap_rfc_destination_info", 1, x.lab(destination=d),
                "Internal RFC destination (value is always 1)")


def m_GetSystemUpdateList(x, reg):
    for i in rows(x.c.call("GetSystemUpdateList"), "instance"):
        reg.add("sapcontrol_rks_instance_status", color(i.get("dispstatus")),
                x.lab(hostname=i.get("hostname", ""), sap_instance_nr=i.get("instanceNr", ""),
                      status=i.get("status", "")),
                "Rolling kernel switch progress per instance: 1=gray 2=green 3=yellow 4=red")


def _depths(nodes):
    depths = []
    for idx in range(len(nodes)):
        d, cur, seen = 0, idx, set()
        while cur not in seen:
            seen.add(cur)
            try:
                p = int(nodes[cur].get("parent", -1))
            except (ValueError, TypeError):
                break
            if p == cur or p < 0 or p >= len(nodes):
                break
            cur = p
            d += 1
        depths.append(d)
    return depths


def _path(nodes, idx):
    names, cur, seen = [], idx, set()
    while cur not in seen:
        seen.add(cur)
        names.append(nodes[cur].get("name", ""))
        try:
            p = int(nodes[cur].get("parent", -1))
        except (ValueError, TypeError):
            break
        if p == cur or p < 0 or p >= len(nodes):
            break
        cur = p
    return "/".join(reversed(names))


def m_GetAlertTree(x, reg):
    nodes = rows(x.c.call("GetAlertTree"), "tree")
    depths = _depths(nodes)
    counts = {}
    for i, (n, d) in enumerate(zip(nodes, depths)):
        state = {1: "gray", 2: "green", 3: "yellow", 4: "red"}.get(color(n.get("ActualValue")), "unknown")
        counts[state] = counts.get(state, 0) + 1
        if d <= x.args.ccms_depth:
            reg.add("sapcontrol_ccms_node_status", color(n.get("ActualValue")),
                    x.lab(path=short(_path(nodes, i), 200), depth=d),
                    "CCMS alert tree node state (RZ20): 1=gray 2=green 3=yellow 4=red")
    for state in ("gray", "green", "yellow", "red"):
        reg.add("sapcontrol_ccms_nodes", counts.get(state, 0), x.lab(state=state),
                "CCMS alert tree nodes by state")


def m_GetAlerts(x, reg):
    nodes = rows(x.c.call("GetAlertTree"), "tree")
    depths = _depths(nodes)
    roots = [n for n, d in zip(nodes, depths) if d == 0 and n.get("Tid")]
    if not roots:
        return
    resp = x.c.call("GetAlerts", {"RootTid": roots[0].get("Tid")})
    alerts = as_list(resp.get("alert")) if isinstance(resp, dict) else []
    counts, bad = {}, []
    for a in alerts:
        state = {1: "gray", 2: "green", 3: "yellow", 4: "red"}.get(color(a.get("Value")), "unknown")
        counts[state] = counts.get(state, 0) + 1
        if state in ("yellow", "red"):
            bad.append(a)
    for state in ("gray", "green", "yellow", "red"):
        reg.add("sapcontrol_ccms_alerts", counts.get(state, 0), x.lab(state=state), "CCMS alerts by state")
    for a in bad[: x.args.max_series]:
        reg.add("sapcontrol_ccms_alert_status", color(a.get("Value")),
                x.lab(object=short(a.get("Object", "")), attribute=short(a.get("Attribute", ""))),
                "Yellow/red CCMS alerts (top entries only)")


def m_Enqueue(x, reg):
    try:
        stat, ver = x.c.call("EnqGetStatistic2"), 2
        stat = stat.get("statistic", stat) if isinstance(stat, dict) else {}
    except SoapError:
        stat, ver = x.c.call("EnqGetStatistic"), 1
        stat = stat.get("statistic", stat) if isinstance(stat, dict) else {}
    for key, val in sorted(stat.items()):
        if key.endswith("_state"):
            reg.add("sapcontrol_enqueue_" + metric_name(key), color(val), x.lab(ensa=ver),
                    "Enqueue server state: 1=gray 2=green 3=yellow 4=red")
            continue
        v = num(val)
        if v is None:
            continue
        counter = re.search(r"(requests|rejects|errors|_error|timeouts|rollouts|rollins|overflows|records|_total)$", key)
        name = "sapcontrol_enqueue_" + metric_name(key)
        if counter and not name.endswith("_total"):
            name += "_total"
        reg.add(name, v, x.lab(ensa=ver), "Enqueue server statistic '%s'" % key, "counter" if counter else "gauge")


def m_EnqGetLockTable(x, reg):
    by_mode, total = {}, 0
    for lk in rows(x.c.call("EnqGetLockTable"), "lock"):
        total += 1
        mode = lk.get("lock_mode", "")
        by_mode[mode] = by_mode.get(mode, 0) + 1
    reg.add("sapcontrol_enqueue_lock_table_entries", total, x.lab(), "Entries in the enqueue lock table")
    for mode, n in sorted(by_mode.items()):
        reg.add("sapcontrol_enqueue_lock_table_entries_by_mode", n, x.lab(lock_mode=mode),
                "Enqueue locks by lock mode")


def m_ICMGetThreadList(x, reg):
    by_status = {}
    for t in rows(x.c.call("ICMGetThreadList"), "thread"):
        st = t.get("status", "")
        by_status[st] = by_status.get(st, 0) + 1
        reg.add("sapcontrol_icm_thread_requests_total", num(t.get("requests")),
                x.lab(name=t.get("name", ""), id=t.get("id", "")), "Requests handled by an ICM thread", "counter")
    for st, n in sorted(by_status.items()):
        reg.add("sapcontrol_icm_threads", n, x.lab(status=st), "ICM threads by status")


def m_ICMGetConnectionList(x, reg):
    counts = {}
    for c in rows(x.c.call("ICMGetConnectionList"), "connection"):
        k = (c.get("protocol", ""), c.get("role", ""))
        counts[k] = counts.get(k, 0) + 1
    for (proto, role), n in sorted(counts.items()):
        reg.add("sapcontrol_icm_connections", n, x.lab(protocol=proto, role=role), "ICM connections")


def m_ICMGetCacheEntries(x, reg):
    cnt, size = {}, {}
    for e in rows(x.c.call("ICMGetCacheEntries"), "entry"):
        c = e.get("cache", "")
        cnt[c] = cnt.get(c, 0) + 1
        size[c] = size.get(c, 0) + (num(e.get("size")) or 0)
    for c in sorted(cnt):
        reg.add("sapcontrol_icm_cache_entries", cnt[c], x.lab(cache=c), "Objects in an ICM cache")
        reg.add("sapcontrol_icm_cache_bytes", size[c], x.lab(cache=c), "Size of an ICM cache")


def m_GWGetConnectionList(x, reg):
    counts = {}
    for c in rows(x.c.call("GWGetConnectionList"), "connection"):
        counts[c.get("state", "")] = counts.get(c.get("state", ""), 0) + 1
    for st, n in sorted(counts.items()):
        reg.add("sapcontrol_gateway_connections", n, x.lab(state=st), "Gateway connections by state")


def m_GWGetClientList(x, reg):
    counts = {}
    for c in rows(x.c.call("GWGetClientList"), "client"):
        k = (c.get("type", ""), c.get("state", ""))
        counts[k] = counts.get(k, 0) + 1
    for (typ, st), n in sorted(counts.items()):
        reg.add("sapcontrol_gateway_clients", n, x.lab(type=typ, state=st), "Gateway clients by type and state")


# Fixed allow-list. Methods that change state (Start, Stop, SendSignal, OSExecute, SetProcessParameter,
# ABAPAcknowledgeAlerts, ABAPSetServerInactive, UpdateSystem, EnqRemoveLocks, GWDelete*, ...) are NOT
# here and there is no option to add them.
COLLECTORS = [
    # name, function, enabled by default
    ("GetProcessList", m_GetProcessList, True),
    ("GetSystemInstanceList", m_GetSystemInstanceList, True),
    ("GetInstanceProperties", m_GetInstanceProperties, True),
    ("GetVersionInfo", m_GetVersionInfo, True),
    ("GetQueueStatistic", m_GetQueueStatistic, True),
    ("GetAccessPointList", m_GetAccessPointList, True),
    ("ABAPGetWPTable", m_ABAPGetWPTable, True),
    ("ABAPGetSystemWPTable", m_ABAPGetSystemWPTable, True),
    ("ABAPReadSyslog", m_ABAPReadSyslog, True),
    ("ABAPGetComponentList", m_ABAPGetComponentList, True),
    ("ABAPCheckRFCDestinations", m_ABAPCheckRFCDestinations, True),
    ("GetSystemUpdateList", m_GetSystemUpdateList, True),
    ("GetAlertTree", m_GetAlertTree, True),
    ("GetAlerts", m_GetAlerts, False),
    ("EnqGetStatistic", m_Enqueue, True),
    ("EnqGetLockTable", m_EnqGetLockTable, True),
    ("ICMGetThreadList", m_ICMGetThreadList, True),
    ("ICMGetConnectionList", m_ICMGetConnectionList, True),
    ("ICMGetCacheEntries", m_ICMGetCacheEntries, False),
    ("GWGetConnectionList", m_GWGetConnectionList, True),
    ("GWGetClientList", m_GWGetClientList, True),
]


# --------------------------------------------------------------------------- scrape
class Exporter(object):
    def __init__(self, args):
        self.args = args
        self.client = SapControl(args)
        self.base = {"sap_instance_nr": args.instance_nr}
        wanted = [m.strip() for m in args.methods.split(",") if m.strip()] if args.methods else None
        skip = set(m.strip() for m in args.skip.split(",") if m.strip())
        self.methods = []
        known = set(n for n, _, _ in COLLECTORS)
        for bad in (set(wanted or []) | skip) - known:
            sys.exit("unknown method '%s'. Allowed: %s" % (bad, ", ".join(sorted(known))))
        for name, fn, default in COLLECTORS:
            if name in skip:
                continue
            if (wanted is not None and name in wanted) or (wanted is None and default):
                self.methods.append((name, fn))
        self.unsupported = set()
        self.lock = threading.Lock()
        self.cached, self.cached_at = "", 0.0

    def collect(self):
        reg = Registry()
        x = Ctx(self.client, self.base, self.args)
        t_all = time.time()
        reachable, errors = True, {}
        for name, fn in self.methods:
            t0 = time.time()
            ok = 1
            if name in self.unsupported:
                reg.add("sapcontrol_method_unsupported", 1, dict(self.base, method=name),
                        "1 if this instance does not offer the method (not counted as a failure)")
                continue
            if not reachable:
                ok = 0
            else:
                try:
                    fn(x, reg)
                except Unsupported as exc:
                    LOG.info("%s is not supported by this instance (%s); skipping it from now on", name, exc)
                    self.unsupported.add(name)
                    reg.add("sapcontrol_method_unsupported", 1, dict(self.base, method=name),
                            "1 if this instance does not offer the method (not counted as a failure)")
                    continue
                except ConnectError as exc:
                    LOG.error("cannot reach sapstartsrv: %s", exc)
                    reachable, ok = False, 0
                    errors[name] = "connection failed"
                except SoapError as exc:
                    LOG.warning("%s failed: %s", name, exc)
                    ok, errors[name] = 0, short(exc, 80)
                except Exception as exc:  # a parsing surprise must not kill the scrape
                    LOG.exception("%s: unexpected error", name)
                    ok, errors[name] = 0, "unexpected: %s" % short(exc, 60)
            reg.add("sapcontrol_method_success", ok, dict(self.base, method=name),
                    "1 if the SAPControl method worked in this scrape")
            reg.add("sapcontrol_method_duration_seconds", time.time() - t0, dict(self.base, method=name),
                    "Time spent on the SAPControl method")
        for name, err in sorted(errors.items()):
            reg.add("sapcontrol_method_error_info", 1, dict(self.base, method=name, error=err),
                    "Last error of a failed method (value is always 1)")
        reg.add("sapcontrol_up", 1 if reachable else 0, self.base, "1 if sapstartsrv could be reached")
        reg.add("sapcontrol_scrape_duration_seconds", time.time() - t_all, self.base, "Duration of the whole scrape")
        reg.add("sapcontrol_exporter_info", 1, dict(self.base, version=VERSION, endpoint=self.client.describe()),
                "Exporter version and connection (value is always 1)")
        return reg.render()

    def metrics(self):
        with self.lock:
            if time.time() - self.cached_at >= self.args.cache_ttl:
                self.cached = self.collect()
                self.cached_at = time.time()
            return self.cached


class Handler(BaseHTTPRequestHandler):
    exporter = None

    def do_GET(self):
        if self.path.startswith("/metrics"):
            body = self.exporter.metrics().encode("utf-8")
            ctype = "text/plain; version=0.0.4; charset=utf-8"
        elif self.path == "/healthz":
            body, ctype = b"ok\n", "text/plain"
        else:
            body = b'<html><body><h1>sapcontrol exporter</h1><a href="/metrics">metrics</a></body></html>'
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        LOG.debug("http: " + fmt, *a)


class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Prometheus exporter for the SAPControl web service")
    p.add_argument("--listen", default=os.environ.get("SAPCONTROL_LISTEN", "127.0.0.1:9681"))
    p.add_argument("--instance-nr", default=os.environ.get("SAPCONTROL_INSTANCE_NR", "00"),
                   help="two-digit SAP instance number, e.g. 00")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="default 5<NR>13 (http) / 5<NR>14 (https)")
    p.add_argument("--https", action="store_true")
    p.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    p.add_argument("--socket", default=os.environ.get("SAPCONTROL_SOCKET", "auto"),
                   help="trusted local Unix socket: a path, 'auto' (/tmp/.sapstream5<NR>13 if it exists) or 'none'")
    p.add_argument("--user", default=os.environ.get("SAPCONTROL_USER"))
    p.add_argument("--password-file", default=os.environ.get("SAPCONTROL_PASSWORD_FILE"))
    p.add_argument("--namespace", default="urn:SAPControl", help="SOAP namespace (targetNamespace of the WSDL)")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--cache-ttl", type=float, default=10.0, help="seconds to reuse a scrape result")
    p.add_argument("--methods", default=os.environ.get("SAPCONTROL_METHODS", ""),
                   help="comma list; replaces the default method set")
    p.add_argument("--skip", default=os.environ.get("SAPCONTROL_SKIP", ""),
                   help="comma list of methods to leave out")
    p.add_argument("--ccms-depth", type=int, default=2)
    p.add_argument("--max-series", type=int, default=50, help="cap for list-style metrics")
    p.add_argument("--once", action="store_true", help="print metrics once and exit")
    p.add_argument("--list-methods", action="store_true")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args(argv)
    a.instance_nr = "%02d" % int(a.instance_nr)
    a.password = os.environ.get("SAPCONTROL_PASSWORD")
    if a.password_file:
        with open(a.password_file) as fh:
            a.password = fh.read().rstrip("\r\n")
    return a


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.list_methods:
        for name, _, default in COLLECTORS:
            print("%-26s %s" % (name, "default" if default else "off by default"))
        return 0
    exp = Exporter(args)
    if args.once:
        sys.stdout.write(exp.collect())
        return 0
    host, _, port = args.listen.rpartition(":")
    Handler.exporter = exp
    srv = ThreadingServer((host or "127.0.0.1", int(port)), Handler)
    LOG.info("sapcontrol_exporter %s on %s, SAPControl via %s, %d methods",
             VERSION, args.listen, exp.client.describe(), len(exp.methods))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())