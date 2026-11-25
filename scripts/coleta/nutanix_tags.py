#!/usr/bin/env python3
"""
Coleta métricas de VMs Nutanix agrupadas pela categoria 'centrodecusto'.

Tabela destino: tag_nutanix
Colunas: datetime, centrodecusto, vms, vcpus_allocated, mem_allocated_gb, disk_total_gb
"""

import urllib3
import requests
import psycopg2
from datetime import datetime, timezone
from collections import defaultdict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import os

# Credenciais e endereços vêm só de variáveis de ambiente (ver .env.example).
PRISM_IP_V3       = os.environ["PRISM_IP_V3"]
PORT_V3           = int(os.environ.get("PORT_V3", "9440"))
USERNAME_V3       = os.environ["USERNAME_V3"]
PASSWORD_V3       = os.environ["PASSWORD_V3"]
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
POSTGRES_USER     = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

BASE_V3 = f"https://{PRISM_IP_V3}:{PORT_V3}/api/nutanix/v3"

CATEGORY_KEY = "centrodecusto"

SESSION = requests.Session()
SESSION.auth = (USERNAME_V3, PASSWORD_V3)
SESSION.verify = False
SESSION.headers.update({"Content-Type": "application/json"})


# ─── Nutanix API ──────────────────────────────────────────────────────────────

def post_v3(path, payload):
    r = SESSION.post(f"{BASE_V3}/{path}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_all_vms():
    """Pagina a API v3 e retorna todas as VMs."""
    vms = []
    offset, length = 0, 500
    while True:
        data = post_v3("vms/list", {
            "kind": "vm",
            "length": length,
            "offset": offset,
            "sort_attribute": "name",
            "sort_order": "ASCENDING",
        })
        entities = data.get("entities", [])
        vms.extend(entities)
        total = data.get("metadata", {}).get("total_matches", 0)
        offset += length
        if offset >= total:
            break
    print(f"[Nutanix] VMs coletadas: {len(vms)}")
    return vms


def collect_by_centrodecusto():
    """Agrupa VMs pelo valor de metadata.categories.centrodecusto.
    Retorna dict vazio se nenhuma VM tiver a categoria."""
    vms = get_all_vms()
    data = defaultdict(lambda: {
        "vms": 0,
        "vcpus_allocated": 0,
        "mem_allocated_gb": 0.0,
        "disk_total_gb": 0.0,
    })

    sem_tag = 0
    for vm in vms:
        cats  = vm.get("metadata", {}).get("categories", {})
        cc    = cats.get(CATEGORY_KEY)
        if not cc:
            sem_tag += 1
            continue

        resources = vm.get("spec", {}).get("resources", {})
        vcpus  = (resources.get("num_vcpus_per_socket", 1) or 1) * \
                 (resources.get("num_sockets", 1) or 1)
        mem_gb = round((resources.get("memory_size_mib", 0) or 0) / 1024, 4)
        disk_gb = sum(
            (d.get("disk_size_mib", 0) or 0) / 1024
            for d in resources.get("disk_list", [])
        )

        data[cc]["vms"]               += 1
        data[cc]["vcpus_allocated"]   += vcpus
        data[cc]["mem_allocated_gb"]  += mem_gb
        data[cc]["disk_total_gb"]     += round(disk_gb, 4)

    if sem_tag:
        print(f"[Nutanix] {sem_tag} VMs sem categoria '{CATEGORY_KEY}' (ignoradas).")
    return data


# ─── PostgreSQL ───────────────────────────────────────────────────────────────

def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tag_nutanix (id SERIAL PRIMARY KEY)")
    conn.commit()

    needed = {
        "datetime":         "TIMESTAMPTZ",
        "centrodecusto":    "TEXT",
        "vms":              "INT",
        "vcpus_allocated":  "INT",
        "mem_allocated_gb": "DOUBLE PRECISION",
        "disk_total_gb":    "DOUBLE PRECISION",
    }
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='tag_nutanix'")
    existing = {r[0] for r in cur.fetchall()}
    for col, typ in needed.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE tag_nutanix ADD COLUMN {col} {typ}")
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
    INSERT INTO tag_nutanix (
        datetime, centrodecusto, vms,
        vcpus_allocated, mem_allocated_gb, disk_total_gb
    ) VALUES (%s,%s,%s,%s,%s,%s)
    """
    for cc, v in data.items():
        cur.execute(q, (
            now, cc, v["vms"],
            v["vcpus_allocated"],
            round(v["mem_allocated_gb"], 4),
            round(v["disk_total_gb"],    4),
        ))
        print(f"  {cc:30s}: {v['vms']} VMs | "
              f"{v['vcpus_allocated']} vCPUs | "
              f"{v['mem_allocated_gb']:.2f} GB RAM | "
              f"{v['disk_total_gb']:.2f} GB Disco")

    conn.commit()
    cur.close()
    conn.close()
    print(f"[Nutanix] {len(data)} centros de custo salvos em tag_nutanix.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def ensure_table_standalone():
    """Garante que a tabela existe mesmo sem dados para inserir."""
    conn = psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        database=POSTGRES_DB, user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    ensure_table(conn)
    conn.close()


if __name__ == "__main__":
    print("=== Coleta Nutanix (categoria centrodecusto) ===")
    ensure_table_standalone()
    d = collect_by_centrodecusto()
    if not d:
        print(f"[Nutanix] Nenhuma VM com categoria '{CATEGORY_KEY}' encontrada.")
        print(f"[Nutanix] Tabela tag_nutanix garantida. Execute tag_nutanix_vms.py --apply para atribuir as tags.")
    else:
        save(d)
    print("[Nutanix] Concluído.")
