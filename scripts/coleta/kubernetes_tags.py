#!/usr/bin/env python3
"""
Coleta métricas de pods Kubernetes agrupadas pelo label 'centrodecusto'.
Fonte: Prometheus (puro HTTP — sem kubeconfig).

Tabela destino: tag_k8s
Colunas: datetime, centrodecusto, pods,
         cpu_limit, cpu_used,
         mem_limit_gb, mem_used_gb,
         pvc_total_gb, pvc_used_gb
"""

import requests
import psycopg2
import urllib3
from datetime import datetime, timezone
from collections import defaultdict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import os

# Credenciais e endereços vêm só de variáveis de ambiente (ver .env.example).
PROMETHEUS_URL    = os.environ["PROMETHEUS_URL"]
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
POSTGRES_USER     = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

SESSION = requests.Session()
SESSION.verify = False


# ─── Prometheus ───────────────────────────────────────────────────────────────

def query(promql):
    """Executa query instantânea no Prometheus. Retorna lista de results."""
    r = SESSION.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": promql},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {data.get('error')}")
    return data["data"]["result"]


def val(result_item):
    """Extrai o valor numérico de um result item do Prometheus."""
    return float(result_item["value"][1])


# ─── Coleta de dados via PromQL ───────────────────────────────────────────────

def get_pod_centrodecusto_map():
    """Retorna dict (namespace, pod) -> centrodecusto para pods com o label."""
    results = query('kube_pod_labels{label_centrodecusto!=""}')
    pod_map = {}
    for r in results:
        m = r["metric"]
        ns   = m.get("namespace", "")
        pod  = m.get("pod", "")
        cc   = m.get("label_centrodecusto", "")
        if ns and pod and cc:
            pod_map[(ns, pod)] = cc
    print(f"[K8s] Pods com centrodecusto: {len(pod_map)}")
    return pod_map


def get_cpu_limits():
    """CPU limit (cores) por pod."""
    results = query(
        'sum by (namespace,pod) '
        '(kube_pod_container_resource_limits{resource="cpu",container!=""})'
    )
    return {
        (r["metric"]["namespace"], r["metric"]["pod"]): val(r)
        for r in results
        if "namespace" in r["metric"] and "pod" in r["metric"]
    }


def get_mem_limits_gb():
    """Mem limit (GB) por pod."""
    results = query(
        'sum by (namespace,pod) '
        '(kube_pod_container_resource_limits{resource="memory",container!=""})'
    )
    return {
        (r["metric"]["namespace"], r["metric"]["pod"]): val(r) / (1024 ** 3)
        for r in results
        if "namespace" in r["metric"] and "pod" in r["metric"]
    }


def get_cpu_used():
    """CPU utilizado (cores, média 1h) por pod."""
    results = query(
        'sum by (namespace,pod) '
        '(rate(container_cpu_usage_seconds_total'
        '{container!="POD",container!=""}[1h]))'
    )
    return {
        (r["metric"]["namespace"], r["metric"]["pod"]): val(r)
        for r in results
        if "namespace" in r["metric"] and "pod" in r["metric"]
    }


def get_mem_used_gb():
    """Mem utilizada (GB, working set) por pod."""
    results = query(
        'sum by (namespace,pod) '
        '(container_memory_working_set_bytes'
        '{container!="POD",container!=""})'
    )
    return {
        (r["metric"]["namespace"], r["metric"]["pod"]): val(r) / (1024 ** 3)
        for r in results
        if "namespace" in r["metric"] and "pod" in r["metric"]
    }


def get_pvc_pod_map():
    """Retorna dict (namespace, pvc) -> pod via kube_pod_spec_volumes_persistentvolumeclaims_info."""
    try:
        results = query("kube_pod_spec_volumes_persistentvolumeclaims_info")
        pvc_map = {}
        for r in results:
            m = r["metric"]
            ns  = m.get("namespace", "")
            pod = m.get("pod", "")
            pvc = m.get("persistentvolumeclaim", "")
            if ns and pod and pvc:
                pvc_map[(ns, pvc)] = pod
        return pvc_map
    except Exception as e:
        print(f"[K8s][AVISO] PVC-pod mapping indisponível: {e}")
        return {}


def get_pvc_total_gb():
    """PVC solicitado (GB) por (namespace, pvc)."""
    results = query("kube_persistentvolumeclaim_resource_requests_storage_bytes")
    return {
        (r["metric"]["namespace"], r["metric"]["persistentvolumeclaim"]): val(r) / (1024 ** 3)
        for r in results
        if "persistentvolumeclaim" in r["metric"] and "namespace" in r["metric"]
    }


def get_pvc_used_gb():
    """PVC utilizado (GB) por (namespace, pvc)."""
    results = query("kubelet_volume_stats_used_bytes")
    return {
        (r["metric"]["namespace"], r["metric"]["persistentvolumeclaim"]): val(r) / (1024 ** 3)
        for r in results
        if "persistentvolumeclaim" in r["metric"] and "namespace" in r["metric"]
    }


# ─── Agregação ────────────────────────────────────────────────────────────────

def collect_by_centrodecusto():
    pod_cc    = get_pod_centrodecusto_map()
    cpu_lim   = get_cpu_limits()
    mem_lim   = get_mem_limits_gb()
    cpu_used  = get_cpu_used()
    mem_used  = get_mem_used_gb()
    pvc_pod   = get_pvc_pod_map()
    pvc_total = get_pvc_total_gb()
    pvc_used  = get_pvc_used_gb()

    data = defaultdict(lambda: {
        "pods": 0,
        "cpu_limit": 0.0,
        "cpu_used": 0.0,
        "mem_limit_gb": 0.0,
        "mem_used_gb": 0.0,
        "pvc_total_gb": 0.0,
        "pvc_used_gb": 0.0,
    })

    # Agrega métricas de pods
    for (ns, pod), cc in pod_cc.items():
        data[cc]["pods"]        += 1
        data[cc]["cpu_limit"]   += cpu_lim.get((ns, pod), 0.0)
        data[cc]["cpu_used"]    += cpu_used.get((ns, pod), 0.0)
        data[cc]["mem_limit_gb"] += mem_lim.get((ns, pod), 0.0)
        data[cc]["mem_used_gb"]  += mem_used.get((ns, pod), 0.0)

    # Agrega PVCs via mapa (ns, pvc) -> pod -> centrodecusto
    for (ns, pvc), pod in pvc_pod.items():
        cc = pod_cc.get((ns, pod))
        if not cc:
            continue
        data[cc]["pvc_total_gb"] += pvc_total.get((ns, pvc), 0.0)
        data[cc]["pvc_used_gb"]  += pvc_used.get((ns, pvc), 0.0)

    return data


# ─── PostgreSQL ───────────────────────────────────────────────────────────────

def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tag_k8s (id SERIAL PRIMARY KEY)")
    conn.commit()

    needed = {
        "datetime":      "TIMESTAMPTZ",
        "centrodecusto": "TEXT",
        "pods":          "INT",
        "cpu_limit":     "DOUBLE PRECISION",
        "cpu_used":      "DOUBLE PRECISION",
        "mem_limit_gb":  "DOUBLE PRECISION",
        "mem_used_gb":   "DOUBLE PRECISION",
        "pvc_total_gb":  "DOUBLE PRECISION",
        "pvc_used_gb":   "DOUBLE PRECISION",
    }

    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='tag_k8s'")
    existing = {r[0] for r in cur.fetchall()}

    for col, typ in needed.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE tag_k8s ADD COLUMN {col} {typ}")
            conn.commit()
    cur.close()


def save(data):
    conn = psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        database=POSTGRES_DB, user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    ensure_table(conn)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    q = """
    INSERT INTO tag_k8s (
        datetime, centrodecusto, pods,
        cpu_limit, cpu_used,
        mem_limit_gb, mem_used_gb,
        pvc_total_gb, pvc_used_gb
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    for cc, v in data.items():
        cur.execute(q, (
            now, cc, v["pods"],
            round(v["cpu_limit"],    4),
            round(v["cpu_used"],     4),
            round(v["mem_limit_gb"], 4),
            round(v["mem_used_gb"],  4),
            round(v["pvc_total_gb"], 4),
            round(v["pvc_used_gb"],  4),
        ))
        print(f"  {cc:30s}: {v['pods']} pods | "
              f"CPU {v['cpu_used']:.2f}/{v['cpu_limit']:.2f} cores | "
              f"RAM {v['mem_used_gb']:.2f}/{v['mem_limit_gb']:.2f} GB | "
              f"PVC {v['pvc_used_gb']:.2f}/{v['pvc_total_gb']:.2f} GB")

    conn.commit()
    cur.close()
    conn.close()
    print(f"[K8s] {len(data)} centros de custo salvos em tag_k8s.")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Coleta K8s (label centrodecusto via Prometheus) ===")
    d = collect_by_centrodecusto()
    if not d:
        print("[K8s] Nenhum pod com label 'centrodecusto' encontrado.")
    else:
        save(d)
    print("[K8s] Concluído.")
