#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
datacenter_collector.py
=======================

Coletor único e consolidado de métricas do Data Center.

Conecta simultaneamente em:
  - VMware vCenter  (pyVmomi)
  - Nutanix Prism   (REST API v2.0)

Coleta:
  * Uso REAL (físico) — CPU, Memória, Storage
  * Capacidade RESILIENTE (Nutanix: após reserva de rebuild)
  * PROVISIONAMENTO (overcommit lógico) — CPU, Memória, Storage, vCPUs

Funcionalidades:
  * Coleta paralela (ThreadPoolExecutor)
  * Agregação por: total, plataforma, cluster, tipo de storage
  * Classificação automática de datastores VMware (FAST/SLOW/IGNORE)
  * Filtro de service-datastores locais ("service-datastore", "nodes", "de800")
  * Persistência histórica em PostgreSQL (snapshots — sem update/delete)
  * Cache JSON em /tmp/datacenter_metrics.json
  * Tratamento robusto de erros + logs
  * Pronto para execução via cron
"""

import os
import sys
import ssl
import json
import time
import atexit
import logging
import warnings
import traceback
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import psycopg2
from psycopg2.extras import execute_values
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

# ============================================================
# CONFIGURAÇÃO
# ============================================================
# Credenciais e endereços vêm só de variáveis de ambiente (ver .env.example).
VCENTER_HOST      = os.environ["VCENTER_HOST"]
VCENTER_USER      = os.environ["VCENTER_USER"]
VCENTER_PASSWORD  = os.environ["VCENTER_PASSWORD"]
NUTANIX_HOST      = os.environ["NUTANIX_HOST"]
NUTANIX_USER      = os.environ["NUTANIX_USER"]
NUTANIX_PASSWORD  = os.environ["NUTANIX_PASSWORD"]
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
POSTGRES_USER     = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

CACHE_FILE       = "/tmp/datacenter_metrics.json"
LOG_FILE         = os.environ.get("DATACENTER_LOG_FILE", "/var/log/datacenter_collector.log")
HTTP_TIMEOUT     = 15
VCENTER_TIMEOUT  = 30
THREADS_TOP      = 8
THREADS_HOST     = 8
NUTANIX_PORT     = 9440

PLATFORM_VMWARE  = "vmware"
PLATFORM_NUTANIX = "nutanix"
STORAGE_FAST     = "FAST"
STORAGE_SLOW     = "SLOW"
STORAGE_IGNORE   = "IGNORE"

# ------------------------------------------------------------
# Classificação de datastores VMware
# ------------------------------------------------------------
# Tokens que excluem o datastore da contagem (LUNs locais do ESXi/boot/scratch)
IGNORE_TOKENS = (
    "service-datastore",
    "nodes",
    "de800",
)

# FAST = discos rápidos
FAST_TOKENS = (
    "vsan", "vxrail",         # vSAN / VxRail
    "purestorage",            # Pure Storage NVMe
    "netapp-ssd", "netapp_ssd",
)

# SLOW = discos de capacidade NL-SAS
SLOW_TOKENS = (
    "netapp-nl", "netapp_nl",
    "_nl_",
)

# Suprime warnings de SSL e configura SSL padrão como inseguro
requests.packages.urllib3.disable_warnings()
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
ssl._create_default_https_context = ssl._create_unverified_context


# ============================================================
# LOGGING
# ============================================================
def setup_logger():
    logger = logging.getLogger("datacenter")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        fh = logging.FileHandler(LOG_FILE)
    except (PermissionError, FileNotFoundError):
        fh = logging.FileHandler("/tmp/datacenter_collector.log")
    fh.setFormatter(fmt); logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt); logger.addHandler(sh)
    return logger

log = setup_logger()


# ============================================================
# HELPERS / CONVERSÕES
# ============================================================
def b_to_gb(b):
    try:    return round(float(b) / (1024 ** 3), 2) if b else 0.0
    except: return 0.0

def hz_to_ghz(hz):
    try:    return round(float(hz) / 1_000_000_000, 2) if hz else 0.0
    except: return 0.0

def mhz_to_ghz(mhz):
    try:    return round(float(mhz) / 1000.0, 2) if mhz else 0.0
    except: return 0.0

def pct(used, total):
    try:    return round((float(used) / float(total)) * 100, 2) if total else 0.0
    except: return 0.0


def classify_vmware_datastore(name):
    """
    Classifica um datastore do VMware em FAST, SLOW ou IGNORE.

    Ordem das regras:
      1. IGNORE — se contém qualquer token de IGNORE_TOKENS
         (service-datastore, nodes, de800)
      2. SLOW   — se contém token de SLOW_TOKENS (NETAPP-NL)
      3. FAST   — se contém token de FAST_TOKENS (vSAN, Pure, NETAPP-SSD)
      4. IGNORE — fallback (LUN sem categoria reconhecida)
    """
    if not name:
        return STORAGE_IGNORE
    lname = name.lower()

    if any(tok in lname for tok in IGNORE_TOKENS):
        return STORAGE_IGNORE
    if any(tok in lname for tok in SLOW_TOKENS):
        return STORAGE_SLOW
    if any(tok in lname for tok in FAST_TOKENS):
        return STORAGE_FAST
    return STORAGE_IGNORE


# ============================================================
# COLETA VMWARE
# ============================================================
def connect_vcenter():
    log.info("Conectando ao vCenter %s ...", VCENTER_HOST)
    si = SmartConnect(
        host=VCENTER_HOST, user=VCENTER_USER, pwd=VCENTER_PASSWORD,
        sslContext=ssl._create_unverified_context(),
        connectionPoolTimeout=VCENTER_TIMEOUT,
    )
    atexit.register(Disconnect, si)
    return si


def build_vm_host_map(content):
    """Indexa VMs por host_moId em um único pass (evita N+1)."""
    vm_map = {}
    try:
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        for vm in view.view:
            try:
                if getattr(vm, "config", None) and getattr(vm.config, "template", False):
                    continue
                host = getattr(vm.runtime, "host", None)
                if host and getattr(host, "_moId", None):
                    vm_map.setdefault(host._moId, []).append(vm)
            except Exception:
                continue
        view.Destroy()
    except Exception as e:
        log.warning("Falha ao mapear VMs->hosts: %s", e)
    return vm_map


def collect_host(host, vm_map, ds_global_seen):
    """
    Coleta métricas de um host ESXi:
      * CPU/Memória físicos
      * Datastores (FAST/SLOW) com used + uncommitted = provisioned
      * VMs: contagem + vCPU/RAM provisionados
    """
    out = {
        "name": getattr(host, "name", "unknown"),
        "cpu_total_cores": 0, "cpu_total_ghz": 0.0, "cpu_used_ghz": 0.0,
        "cpu_provisioned_ghz": 0.0,
        "memory_total_gb": 0.0, "memory_used_gb": 0.0,
        "memory_provisioned_gb": 0.0,
        "storage_fast_total_gb": 0.0, "storage_fast_used_gb": 0.0,
        "storage_fast_provisioned_gb": 0.0,
        "storage_fast_resilient_gb": 0.0, "storage_fast_resilient_used_gb": 0.0,
        "storage_slow_total_gb": 0.0, "storage_slow_used_gb": 0.0,
        "storage_slow_provisioned_gb": 0.0,
        "storage_slow_resilient_gb": 0.0, "storage_slow_resilient_used_gb": 0.0,
        "vm_total": 0, "vm_on": 0, "vm_off": 0,
        "vcpu_total": 0, "vcpu_on": 0, "vcpu_off": 0,
        "vcpu_provisioned": 0,
    }
    try:
        hw = getattr(host, "hardware", None)
        summary = getattr(host, "summary", None)

        # ---------- CPU ----------
        if hw and getattr(hw, "cpuInfo", None):
            out["cpu_total_cores"] = int(getattr(hw.cpuInfo, "numCpuCores", 0))
            hz = float(getattr(hw.cpuInfo, "hz", 0))
            out["cpu_total_ghz"] = round(out["cpu_total_cores"] * (hz / 1e9), 2) if hz else 0.0

        if summary and getattr(summary, "quickStats", None):
            out["cpu_used_ghz"]   = mhz_to_ghz(getattr(summary.quickStats, "overallCpuUsage", 0))
            out["memory_used_gb"] = round(float(getattr(summary.quickStats, "overallMemoryUsage", 0)) / 1024.0, 2)

        if hw:
            out["memory_total_gb"] = b_to_gb(getattr(hw, "memorySize", 0))

        # GHz por core (usado para calcular CPU provisionado a partir de vCPUs)
        ghz_per_core = (out["cpu_total_ghz"] / out["cpu_total_cores"]) \
                        if out["cpu_total_cores"] > 0 else 0.0

        # ---------- DATASTORES ----------
        # used + uncommitted = provisioned (fórmula clássica de overcommit)
        # ds_global_seen evita contar datastore compartilhado duas vezes.
        for ds in getattr(host, "datastore", []) or []:
            try:
                ds_id = getattr(ds, "_moId", None)
                if not ds_id or ds_id in ds_global_seen:
                    continue
                ds_global_seen.add(ds_id)
                ds_name = getattr(ds, "name", "") or ""

                kind = classify_vmware_datastore(ds_name)
                if kind == STORAGE_IGNORE:
                    log.debug("Ignorando datastore '%s'", ds_name)
                    continue

                cap         = float(getattr(ds.summary, "capacity", 0))  / (1024 ** 3)
                free        = float(getattr(ds.summary, "freeSpace", 0)) / (1024 ** 3)
                uncommitted = float(getattr(ds.summary, "uncommitted", 0) or 0) / (1024 ** 3)
                used        = max(cap - free, 0.0)
                provisioned = used + uncommitted   # << provisionamento

                if kind == STORAGE_FAST:
                    out["storage_fast_total_gb"]          += cap
                    out["storage_fast_used_gb"]           += used
                    out["storage_fast_provisioned_gb"]    += provisioned
                    # VMware: resilient = bruto (vSAN/NetApp não têm reserva equivalente)
                    out["storage_fast_resilient_gb"]      += cap
                    out["storage_fast_resilient_used_gb"] += used
                else:  # SLOW
                    out["storage_slow_total_gb"]          += cap
                    out["storage_slow_used_gb"]           += used
                    out["storage_slow_provisioned_gb"]    += provisioned
                    out["storage_slow_resilient_gb"]      += cap
                    out["storage_slow_resilient_used_gb"] += used
            except Exception:
                continue

        # ---------- VMs / PROVISIONAMENTO de Compute ----------
        for vm in vm_map.get(getattr(host, "_moId", ""), []):
            try:
                cfg = getattr(vm, "config", None)
                if cfg and getattr(cfg, "template", False):
                    continue

                out["vm_total"] += 1
                vcpus   = int(getattr(cfg.hardware, "numCPU", 0))    if cfg else 0
                vmem_mb = int(getattr(cfg.hardware, "memoryMB", 0))  if cfg else 0

                out["vcpu_total"]            += vcpus
                out["vcpu_provisioned"]      += vcpus
                out["memory_provisioned_gb"] += vmem_mb / 1024.0
                out["cpu_provisioned_ghz"]   += vcpus * ghz_per_core

                if getattr(vm.runtime, "powerState", "") == "poweredOn":
                    out["vm_on"]   += 1
                    out["vcpu_on"] += vcpus
                else:
                    out["vm_off"]   += 1
                    out["vcpu_off"] += vcpus
            except Exception:
                continue

    except Exception as e:
        log.warning("Erro coletando host %s: %s", out["name"], e)
    return out


def collect_vmware_cluster(cluster, vm_map, ds_global_seen):
    name = getattr(cluster, "name", "unknown")
    hosts = list(getattr(cluster, "host", [])) or []
    log.info("VMware cluster '%s' — %d hosts", name, len(hosts))

    hosts_data = []
    if hosts:
        with ThreadPoolExecutor(max_workers=min(len(hosts), THREADS_HOST)) as ex:
            futs = {ex.submit(collect_host, h, vm_map, ds_global_seen): h for h in hosts}
            for f in as_completed(futs):
                try:
                    hosts_data.append(f.result())
                except Exception as e:
                    log.warning("Host falhou: %s", e)

    agg = aggregate_hosts(hosts_data)
    agg["name"]        = name
    agg["cluster"]     = name
    agg["plataforma"]  = PLATFORM_VMWARE
    agg["hosts_count"] = len(hosts_data)
    return agg


def collect_vmware():
    """Coleta completa do vCenter, retorna lista de clusters."""
    try:
        si = connect_vcenter()
        content = si.RetrieveContent()
        vm_map = build_vm_host_map(content)

        clusters = []
        for dc in getattr(content.rootFolder, "childEntity", []) or []:
            hf = getattr(dc, "hostFolder", None)
            if not hf:
                continue
            for c in getattr(hf, "childEntity", []) or []:
                if isinstance(c, vim.ClusterComputeResource):
                    clusters.append(c)

        ds_global_seen = set()

        out = []
        with ThreadPoolExecutor(max_workers=THREADS_TOP) as ex:
            futs = {ex.submit(collect_vmware_cluster, c, vm_map, ds_global_seen): c
                    for c in clusters}
            for f in as_completed(futs):
                try:
                    out.append(f.result())
                except Exception as e:
                    log.warning("Cluster VMware falhou: %s", e)
        return out
    except Exception as e:
        log.error("Falha geral VMware: %s\n%s", e, traceback.format_exc())
        return []


# ============================================================
# COLETA NUTANIX
# ============================================================
def nutanix_session():
    s = requests.Session()
    s.auth = (NUTANIX_USER, NUTANIX_PASSWORD)
    s.verify = False
    s.headers.update({"Accept": "application/json"})
    return s


def nutanix_get(session, url):
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        log.warning("Nutanix GET %s -> HTTP %s", url, r.status_code)
    except Exception as e:
        log.warning("Nutanix GET %s falhou: %s", url, e)
    return {}


def collect_nutanix():
    """
    Coleta do Prism Element v2.0.

    Capacidades:
      * BRUTA       = storage_tier.ssd.capacity_bytes (= 'Total Capacity' no Prism)
      * RESILIENTE  = storage.capacity_bytes        (= 'Resilient Capacity' no Prism)
      * USADO       = storage_tier.ssd.usage_bytes  (bruto)
      * USADO_RESIL = storage.usage_bytes

    Provisionamento:
      * vCPU/RAM = soma de num_vcpus / memory_mb das VMs
      * CPU GHz  = vcpus × (GHz por core físico do cluster)
      * Storage  = soma de reservas dos storage_containers
    """
    base = f"https://{NUTANIX_HOST}:{NUTANIX_PORT}/PrismGateway/services/rest/v2.0"
    try:
        sess = nutanix_session()
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_cluster = ex.submit(nutanix_get, sess, f"{base}/cluster")
            f_hosts   = ex.submit(nutanix_get, sess, f"{base}/hosts")
            f_vms     = ex.submit(nutanix_get, sess, f"{base}/vms")
            f_sc      = ex.submit(nutanix_get, sess, f"{base}/storage_containers")
            cluster   = f_cluster.result() or {}
            hosts     = (f_hosts.result() or {}).get("entities", []) or []
            vms       = (f_vms.result() or {}).get("entities", []) or []
            storage_c = (f_sc.result() or {}).get("entities", []) or []

        if not cluster and not hosts:
            log.warning("Nutanix retornou vazio.")
            return []

        c_name = cluster.get("name", "Nutanix-Cluster")
        stats  = cluster.get("stats", {}) or {}
        usage  = cluster.get("usage_stats") or cluster.get("usageStats") or {}

        # ---------- CPU ----------
        cpu_total_hz   = sum(h.get("cpu_capacity_in_hz", 0) for h in hosts)
        cpu_total_ghz  = hz_to_ghz(cpu_total_hz)
        cpu_total_cores = sum(int(h.get("num_cpu_cores", 0) or 0) for h in hosts)
        cpu_usage_pct  = float(stats.get("hypervisor_cpu_usage_ppm", 0)) / 10000.0
        cpu_used_ghz   = round(cpu_total_ghz * cpu_usage_pct / 100.0, 2)

        # ---------- MEMÓRIA ----------
        mem_total_bytes = sum(h.get("memory_capacity_in_bytes", 0) for h in hosts)
        mem_total_gb    = b_to_gb(mem_total_bytes)
        mem_usage_pct   = float(stats.get("hypervisor_memory_usage_ppm", 0)) / 10000.0
        mem_used_gb     = round(mem_total_gb * mem_usage_pct / 100.0, 2)

        # ---------- STORAGE BRUTO (Total Capacity do Prism) ----------
        # AOS 7.x usa storage_tier.ssd.capacity_bytes; AOS antigo usa user_*
        storage_total_gb = 0.0
        storage_used_gb  = 0.0
        STORAGE_KEY_PAIRS = (
            ("storage_tier.ssd.capacity_bytes",  "storage_tier.ssd.usage_bytes"),
            ("storage.user_capacity_bytes",      "storage.user_usage_bytes"),
        )
        for total_k, used_k in STORAGE_KEY_PAIRS:
            total_b = float(usage.get(total_k, 0) or 0)
            used_b  = float(usage.get(used_k, 0)  or 0)
            if total_b > 0:
                storage_total_gb = total_b / (1024 ** 3)
                storage_used_gb  = used_b  / (1024 ** 3)
                log.info("Nutanix bruto via %s: %.2f TiB / %.2f TiB usado",
                         total_k, storage_total_gb/1024, storage_used_gb/1024)
                break

        # Fallback: somar storage_containers se nada veio
        if storage_total_gb == 0:
            log.warning("Nutanix: caindo em storage_containers")
            seen_caps = set()
            for sc in storage_c:
                u = sc.get("usage_stats", {}) or {}
                total_b = float(u.get("storage.user_capacity_bytes", 0) or 0)
                used_b  = float(u.get("storage.user_usage_bytes", 0)   or 0)
                if total_b > 0 and total_b not in seen_caps:
                    seen_caps.add(total_b)
                    storage_total_gb += total_b / (1024 ** 3)
                    storage_used_gb  += used_b  / (1024 ** 3)

        # ---------- STORAGE RESILIENTE (após reserva de rebuild) ----------
        resilient_total_gb = storage_total_gb
        resilient_used_gb  = storage_used_gb
        r_total_b = float(usage.get("storage.capacity_bytes", 0) or 0)
        r_used_b  = float(usage.get("storage.usage_bytes", 0)    or 0)
        if r_total_b > 0:
            resilient_total_gb = r_total_b / (1024 ** 3)
            resilient_used_gb  = r_used_b  / (1024 ** 3)
            log.info("Nutanix resiliente: %.2f TiB / %.2f TiB usado",
                     resilient_total_gb/1024, resilient_used_gb/1024)

        # ---------- VMs / PROVISIONAMENTO de Compute ----------
        vm_total = vm_on = vm_off = 0
        vcpu_total = vcpu_on = vcpu_off = 0
        vcpu_provisioned = 0
        mem_provisioned_gb = 0.0
        for vm in vms:
            vm_total += 1
            vcpus   = int(vm.get("num_vcpus", 0) or 0)
            mem_mib = float(vm.get("memory_mb", 0) or 0)
            vcpu_total            += vcpus
            vcpu_provisioned      += vcpus
            mem_provisioned_gb    += mem_mib / 1024.0
            if str(vm.get("power_state", "")).lower() == "on":
                vm_on  += 1; vcpu_on += vcpus
            else:
                vm_off += 1; vcpu_off += vcpus

        # CPU provisionada (GHz) = vCPUs × (GHz por core físico)
        cpu_provisioned_ghz = 0.0
        if cpu_total_cores > 0 and cpu_total_ghz > 0:
            ghz_per_core = cpu_total_ghz / cpu_total_cores
            cpu_provisioned_ghz = round(vcpu_provisioned * ghz_per_core, 2)

        # ---------- STORAGE PROVISIONADO ----------
        # Soma do "reserved" / "advertised" dos storage_containers reais
        storage_provisioned_gb = 0.0
        for sc in storage_c:
            u = sc.get("usage_stats", {}) or {}
            for k in ("storage.user_reserved_capacity_bytes",
                      "storage.reserved_capacity_bytes"):
                v = float(u.get(k, 0) or 0)
                if v > 0:
                    storage_provisioned_gb += v / (1024 ** 3)
                    break
        # Se não há reservation, usa o "used" como floor
        if storage_provisioned_gb == 0:
            storage_provisioned_gb = storage_used_gb

        return [{
            "name": c_name,
            "cluster": c_name,
            "plataforma": PLATFORM_NUTANIX,
            "hosts_count": len(hosts),
            "cpu_total_cores": cpu_total_cores,
            "cpu_total_ghz": cpu_total_ghz,
            "cpu_used_ghz":  cpu_used_ghz,
            "cpu_provisioned_ghz": cpu_provisioned_ghz,
            "memory_total_gb": round(mem_total_gb, 2),
            "memory_used_gb":  round(mem_used_gb, 2),
            "memory_provisioned_gb": round(mem_provisioned_gb, 2),
            "storage_fast_total_gb":       round(storage_total_gb, 2),
            "storage_fast_used_gb":        round(storage_used_gb, 2),
            "storage_fast_provisioned_gb": round(storage_provisioned_gb, 2),
            "storage_fast_resilient_gb":      round(resilient_total_gb, 2),
            "storage_fast_resilient_used_gb": round(resilient_used_gb, 2),
            "storage_slow_total_gb": 0.0,
            "storage_slow_used_gb":  0.0,
            "storage_slow_provisioned_gb": 0.0,
            "storage_slow_resilient_gb":      0.0,
            "storage_slow_resilient_used_gb": 0.0,
            "vm_total": vm_total, "vm_on": vm_on, "vm_off": vm_off,
            "vcpu_total": vcpu_total, "vcpu_on": vcpu_on, "vcpu_off": vcpu_off,
            "vcpu_provisioned": vcpu_provisioned,
        }]
    except Exception as e:
        log.error("Falha geral Nutanix: %s\n%s", e, traceback.format_exc())
        return []


# ============================================================
# AGREGAÇÕES
# ============================================================
NUMERIC_FIELDS = (
    "cpu_total_cores", "cpu_total_ghz", "cpu_used_ghz", "cpu_provisioned_ghz",
    "memory_total_gb", "memory_used_gb", "memory_provisioned_gb",
    "storage_fast_total_gb", "storage_fast_used_gb", "storage_fast_provisioned_gb",
    "storage_fast_resilient_gb", "storage_fast_resilient_used_gb",
    "storage_slow_total_gb", "storage_slow_used_gb", "storage_slow_provisioned_gb",
    "storage_slow_resilient_gb", "storage_slow_resilient_used_gb",
    "vm_total", "vm_on", "vm_off",
    "vcpu_total", "vcpu_on", "vcpu_off", "vcpu_provisioned",
)

INT_FIELDS = {"cpu_total_cores", "vm_total", "vm_on", "vm_off",
              "vcpu_total", "vcpu_on", "vcpu_off", "vcpu_provisioned"}


def aggregate_hosts(hosts):
    agg = {f: 0 if f in INT_FIELDS else 0.0 for f in NUMERIC_FIELDS}
    for h in hosts:
        for f in NUMERIC_FIELDS:
            agg[f] += h.get(f, 0) or 0
    for f in NUMERIC_FIELDS:
        if f not in INT_FIELDS:
            agg[f] = round(float(agg[f]), 2)
    return agg


def aggregate_clusters(clusters):
    return aggregate_hosts(clusters)


def enrich_with_derived(d):
    """Acrescenta campos derivados (free, percent, provisioned_percent)."""
    d["cpu_free_ghz"]            = round(max(d["cpu_total_ghz"] - d["cpu_used_ghz"], 0), 2)
    d["cpu_usage_percent"]       = pct(d["cpu_used_ghz"], d["cpu_total_ghz"])
    d["cpu_provisioned_percent"] = pct(d.get("cpu_provisioned_ghz", 0), d["cpu_total_ghz"])

    d["memory_free_gb"]            = round(max(d["memory_total_gb"] - d["memory_used_gb"], 0), 2)
    d["memory_usage_percent"]      = pct(d["memory_used_gb"], d["memory_total_gb"])
    d["memory_provisioned_percent"] = pct(d.get("memory_provisioned_gb", 0), d["memory_total_gb"])

    d["storage_fast_free_gb"]            = round(max(d["storage_fast_total_gb"] - d["storage_fast_used_gb"], 0), 2)
    d["storage_fast_usage_percent"]      = pct(d["storage_fast_used_gb"], d["storage_fast_total_gb"])
    d["storage_fast_provisioned_percent"] = pct(d.get("storage_fast_provisioned_gb", 0), d["storage_fast_total_gb"])

    d["storage_slow_free_gb"]            = round(max(d["storage_slow_total_gb"] - d["storage_slow_used_gb"], 0), 2)
    d["storage_slow_usage_percent"]      = pct(d["storage_slow_used_gb"], d["storage_slow_total_gb"])
    d["storage_slow_provisioned_percent"] = pct(d.get("storage_slow_provisioned_gb", 0), d["storage_slow_total_gb"])

    d.setdefault("storage_fast_resilient_gb", 0.0)
    d.setdefault("storage_fast_resilient_used_gb", 0.0)
    d["storage_fast_resilient_free_gb"]       = round(
        max(d["storage_fast_resilient_gb"] - d["storage_fast_resilient_used_gb"], 0), 2)
    d["storage_fast_resilient_usage_percent"] = pct(
        d["storage_fast_resilient_used_gb"], d["storage_fast_resilient_gb"])

    d.setdefault("storage_slow_resilient_gb", 0.0)
    d.setdefault("storage_slow_resilient_used_gb", 0.0)
    d["storage_slow_resilient_free_gb"]       = round(
        max(d["storage_slow_resilient_gb"] - d["storage_slow_resilient_used_gb"], 0), 2)
    d["storage_slow_resilient_usage_percent"] = pct(
        d["storage_slow_resilient_used_gb"], d["storage_slow_resilient_gb"])

    return d


# ============================================================
# JSON CACHE
# ============================================================
def build_json_payload(timestamp, total, vmware_agg, nutanix_agg, clusters):
    return {
        "timestamp": timestamp,
        "summary": {
            "cpu": {
                "total_cores": total["cpu_total_cores"],
                "total_ghz":   total["cpu_total_ghz"],
                "used_ghz":    total["cpu_used_ghz"],
                "free_ghz":    total["cpu_free_ghz"],
                "usage_percent":       total["cpu_usage_percent"],
                "provisioned_ghz":     total["cpu_provisioned_ghz"],
                "provisioned_percent": total["cpu_provisioned_percent"],
            },
            "memory": {
                "total_gb": total["memory_total_gb"],
                "used_gb":  total["memory_used_gb"],
                "free_gb":  total["memory_free_gb"],
                "usage_percent":       total["memory_usage_percent"],
                "provisioned_gb":      total["memory_provisioned_gb"],
                "provisioned_percent": total["memory_provisioned_percent"],
            },
            "storage_fast": {
                "total_gb": total["storage_fast_total_gb"],
                "used_gb":  total["storage_fast_used_gb"],
                "free_gb":  total["storage_fast_free_gb"],
                "usage_percent":       total["storage_fast_usage_percent"],
                "provisioned_gb":      total["storage_fast_provisioned_gb"],
                "provisioned_percent": total["storage_fast_provisioned_percent"],
                "resilient_gb":              total["storage_fast_resilient_gb"],
                "resilient_used_gb":         total["storage_fast_resilient_used_gb"],
                "resilient_free_gb":         total["storage_fast_resilient_free_gb"],
                "resilient_usage_percent":   total["storage_fast_resilient_usage_percent"],
            },
            "storage_slow": {
                "total_gb": total["storage_slow_total_gb"],
                "used_gb":  total["storage_slow_used_gb"],
                "free_gb":  total["storage_slow_free_gb"],
                "usage_percent":       total["storage_slow_usage_percent"],
                "provisioned_gb":      total["storage_slow_provisioned_gb"],
                "provisioned_percent": total["storage_slow_provisioned_percent"],
                "resilient_gb":              total["storage_slow_resilient_gb"],
                "resilient_used_gb":         total["storage_slow_resilient_used_gb"],
                "resilient_free_gb":         total["storage_slow_resilient_free_gb"],
                "resilient_usage_percent":   total["storage_slow_resilient_usage_percent"],
            },
            "vms": {
                "total":            total["vm_total"],
                "on":               total["vm_on"],
                "off":              total["vm_off"],
                "vcpu_total":       total["vcpu_total"],
                "vcpu_on":          total["vcpu_on"],
                "vcpu_off":         total["vcpu_off"],
                "vcpu_provisioned": total["vcpu_provisioned"],
            },
        },
        "platforms": {
            "vmware":  vmware_agg,
            "nutanix": nutanix_agg,
        },
        "clusters": clusters,
    }


# ============================================================
# PERSISTÊNCIA POSTGRES
# ============================================================
def pg_connect():
    return psycopg2.connect(
        host=POSTGRES_HOST, dbname=POSTGRES_DB,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD,
        port=POSTGRES_PORT, connect_timeout=10,
    )


INSERT_SQL = """
INSERT INTO datacenter (
    data_coleta, plataforma, cluster, tipo_storage, scope, name,
    cpu_total_cores, cpu_total_ghz, cpu_used_ghz, cpu_free_ghz, cpu_usage_percent,
    cpu_provisioned_ghz, cpu_provisioned_percent,
    memory_total_gb, memory_used_gb, memory_free_gb, memory_usage_percent,
    memory_provisioned_gb, memory_provisioned_percent,
    storage_fast_total_gb, storage_fast_used_gb, storage_fast_free_gb, storage_fast_usage_percent,
    storage_fast_provisioned_gb, storage_fast_provisioned_percent,
    storage_fast_resilient_gb, storage_fast_resilient_used_gb,
    storage_fast_resilient_free_gb, storage_fast_resilient_usage_percent,
    storage_slow_total_gb, storage_slow_used_gb, storage_slow_free_gb, storage_slow_usage_percent,
    storage_slow_provisioned_gb, storage_slow_provisioned_percent,
    storage_slow_resilient_gb, storage_slow_resilient_used_gb,
    storage_slow_resilient_free_gb, storage_slow_resilient_usage_percent,
    vm_total, vm_on, vm_off,
    vcpu_total, vcpu_on, vcpu_off, vcpu_provisioned
) VALUES %s
"""


def row_from_dict(ts, d, plataforma, cluster, tipo_storage, scope, name):
    return (
        ts, plataforma, cluster, tipo_storage, scope, name,
        int(d.get("cpu_total_cores", 0) or 0),
        float(d.get("cpu_total_ghz", 0) or 0),
        float(d.get("cpu_used_ghz", 0) or 0),
        float(d.get("cpu_free_ghz", 0) or 0),
        float(d.get("cpu_usage_percent", 0) or 0),
        float(d.get("cpu_provisioned_ghz", 0) or 0),
        float(d.get("cpu_provisioned_percent", 0) or 0),
        float(d.get("memory_total_gb", 0) or 0),
        float(d.get("memory_used_gb", 0) or 0),
        float(d.get("memory_free_gb", 0) or 0),
        float(d.get("memory_usage_percent", 0) or 0),
        float(d.get("memory_provisioned_gb", 0) or 0),
        float(d.get("memory_provisioned_percent", 0) or 0),
        float(d.get("storage_fast_total_gb", 0) or 0),
        float(d.get("storage_fast_used_gb", 0) or 0),
        float(d.get("storage_fast_free_gb", 0) or 0),
        float(d.get("storage_fast_usage_percent", 0) or 0),
        float(d.get("storage_fast_provisioned_gb", 0) or 0),
        float(d.get("storage_fast_provisioned_percent", 0) or 0),
        float(d.get("storage_fast_resilient_gb", 0) or 0),
        float(d.get("storage_fast_resilient_used_gb", 0) or 0),
        float(d.get("storage_fast_resilient_free_gb", 0) or 0),
        float(d.get("storage_fast_resilient_usage_percent", 0) or 0),
        float(d.get("storage_slow_total_gb", 0) or 0),
        float(d.get("storage_slow_used_gb", 0) or 0),
        float(d.get("storage_slow_free_gb", 0) or 0),
        float(d.get("storage_slow_usage_percent", 0) or 0),
        float(d.get("storage_slow_provisioned_gb", 0) or 0),
        float(d.get("storage_slow_provisioned_percent", 0) or 0),
        float(d.get("storage_slow_resilient_gb", 0) or 0),
        float(d.get("storage_slow_resilient_used_gb", 0) or 0),
        float(d.get("storage_slow_resilient_free_gb", 0) or 0),
        float(d.get("storage_slow_resilient_usage_percent", 0) or 0),
        int(d.get("vm_total", 0) or 0),
        int(d.get("vm_on", 0) or 0),
        int(d.get("vm_off", 0) or 0),
        int(d.get("vcpu_total", 0) or 0),
        int(d.get("vcpu_on", 0) or 0),
        int(d.get("vcpu_off", 0) or 0),
        int(d.get("vcpu_provisioned", 0) or 0),
    )


def persist(ts, total, vmware_agg, nutanix_agg, clusters):
    """
    Persiste snapshot no PostgreSQL com 4 scopes:
      - TOTAL    : agregação total geral
      - PLATFORM : 1 linha VMware + 1 linha Nutanix
      - CLUSTER  : 1 linha por cluster
      - STORAGE  : 2 linhas (FAST + SLOW)
    """
    rows = []

    rows.append(row_from_dict(ts, total,
        plataforma="ALL", cluster="ALL", tipo_storage="ALL",
        scope="TOTAL", name="Data Center"))

    rows.append(row_from_dict(ts, vmware_agg,
        plataforma=PLATFORM_VMWARE, cluster="ALL", tipo_storage="ALL",
        scope="PLATFORM", name="VMware"))
    rows.append(row_from_dict(ts, nutanix_agg,
        plataforma=PLATFORM_NUTANIX, cluster="ALL", tipo_storage="ALL",
        scope="PLATFORM", name="Nutanix"))

    for c in clusters:
        rows.append(row_from_dict(ts, c,
            plataforma=c["plataforma"], cluster=c["cluster"],
            tipo_storage="ALL", scope="CLUSTER", name=c["name"]))

    fast = {
        "storage_fast_total_gb":           total["storage_fast_total_gb"],
        "storage_fast_used_gb":            total["storage_fast_used_gb"],
        "storage_fast_free_gb":            total["storage_fast_free_gb"],
        "storage_fast_usage_percent":      total["storage_fast_usage_percent"],
        "storage_fast_provisioned_gb":     total["storage_fast_provisioned_gb"],
        "storage_fast_provisioned_percent": total["storage_fast_provisioned_percent"],
        "storage_fast_resilient_gb":           total["storage_fast_resilient_gb"],
        "storage_fast_resilient_used_gb":      total["storage_fast_resilient_used_gb"],
        "storage_fast_resilient_free_gb":      total["storage_fast_resilient_free_gb"],
        "storage_fast_resilient_usage_percent": total["storage_fast_resilient_usage_percent"],
    }
    slow = {
        "storage_slow_total_gb":           total["storage_slow_total_gb"],
        "storage_slow_used_gb":            total["storage_slow_used_gb"],
        "storage_slow_free_gb":            total["storage_slow_free_gb"],
        "storage_slow_usage_percent":      total["storage_slow_usage_percent"],
        "storage_slow_provisioned_gb":     total["storage_slow_provisioned_gb"],
        "storage_slow_provisioned_percent": total["storage_slow_provisioned_percent"],
        "storage_slow_resilient_gb":           total["storage_slow_resilient_gb"],
        "storage_slow_resilient_used_gb":      total["storage_slow_resilient_used_gb"],
        "storage_slow_resilient_free_gb":      total["storage_slow_resilient_free_gb"],
        "storage_slow_resilient_usage_percent": total["storage_slow_resilient_usage_percent"],
    }
    rows.append(row_from_dict(ts, fast, "ALL", "ALL", STORAGE_FAST, "STORAGE", "Storage FAST"))
    rows.append(row_from_dict(ts, slow, "ALL", "ALL", STORAGE_SLOW, "STORAGE", "Storage SLOW"))

    conn = pg_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                execute_values(cur, INSERT_SQL, rows, page_size=200)
        log.info("PostgreSQL: %d linhas inseridas.", len(rows))
    finally:
        conn.close()


# ============================================================
# MAIN
# ============================================================
def main():
    t0 = time.time()
    ts = datetime.now(timezone.utc).isoformat()
    log.info("=== Início da coleta: %s ===", ts)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_vmw = ex.submit(collect_vmware)
        f_nut = ex.submit(collect_nutanix)
        vmware_clusters  = f_vmw.result() or []
        nutanix_clusters = f_nut.result() or []

    log.info("Coletados %d clusters VMware e %d clusters Nutanix.",
             len(vmware_clusters), len(nutanix_clusters))

    all_clusters = vmware_clusters + nutanix_clusters
    if not all_clusters:
        log.error("Nenhum dado coletado. Abortando persistência.")
        return 2

    vmware_agg  = enrich_with_derived(aggregate_clusters(vmware_clusters))  if vmware_clusters  else enrich_with_derived(aggregate_clusters([]))
    nutanix_agg = enrich_with_derived(aggregate_clusters(nutanix_clusters)) if nutanix_clusters else enrich_with_derived(aggregate_clusters([]))
    total       = enrich_with_derived(aggregate_clusters(all_clusters))

    clusters_enriched = [enrich_with_derived(dict(c)) for c in all_clusters]

    vmware_agg["plataforma"]  = PLATFORM_VMWARE
    nutanix_agg["plataforma"] = PLATFORM_NUTANIX

    payload = build_json_payload(ts, total, vmware_agg, nutanix_agg, clusters_enriched)
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.chmod(CACHE_FILE, 0o644)
        log.info("JSON salvo em %s", CACHE_FILE)
    except Exception as e:
        log.warning("Falha ao gravar cache JSON: %s", e)

    try:
        persist(ts, total, vmware_agg, nutanix_agg, clusters_enriched)
    except Exception as e:
        log.error("Falha ao persistir no PostgreSQL: %s\n%s", e, traceback.format_exc())
        return 3

    log.info("=== Fim. Duração: %.2fs ===", time.time() - t0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário.")
        sys.exit(130)
    except Exception as e:
        log.error("Erro fatal: %s\n%s", e, traceback.format_exc())
        sys.exit(1)