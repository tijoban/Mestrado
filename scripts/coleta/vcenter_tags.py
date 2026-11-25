#!/usr/bin/env python3
"""
Coleta métricas de VMs vCenter agrupadas pela tag vSphere da categoria 'Orgao'.
Cada valor da tag (ORGAO_A, ORGAO_B, TESTE...) representa um órgão/cliente.

Tabela destino: tag_vcenter
Colunas: datetime, orgao, vms, vcpus, mem_gb, disk_provisioned_gb, disk_used_gb

REQUISITO DE PERMISSÃO:
  Tagging → vSphere Tagging → Get Attached Tags on Object
  (Administration → Access Control → Global Permissions)
"""

import sys
import ssl
import urllib3
import requests
import psycopg2
from datetime import datetime, timezone
from collections import defaultdict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    print("[ERRO] Instale: pip install pyVmomi")
    sys.exit(1)

import os

# Credenciais e endereços vêm só de variáveis de ambiente (ver .env.example).
VCENTER_HOST      = os.environ["VCENTER_HOST"]
VCENTER_USER      = os.environ["VCENTER_USER"]
VCENTER_PASSWORD  = os.environ["VCENTER_PASSWORD"]
VCENTER_PORT      = int(os.environ.get("VCENTER_PORT", "443"))
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
POSTGRES_USER     = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

TAG_CATEGORY_NAME = "Orgao"


# ─── REST Tag API ─────────────────────────────────────────────────────────────

class VCenterTagAPI:
    """Abstrai a vSphere REST Tag API (/api/ para 7.x+, /rest/ como fallback)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.verify = False
        self._base = None

    def login(self):
        for base, path, key in [
            (f"https://{VCENTER_HOST}/api",  "session",                None),
            (f"https://{VCENTER_HOST}/rest", "com/vmware/cis/session", "value"),
        ]:
            try:
                r = self.s.post(f"{base}/{path}", auth=(VCENTER_USER, VCENTER_PASSWORD), timeout=10)
                if r.ok:
                    token = r.json() if key is None else r.json().get(key)
                    self.s.headers.update({"vmware-api-session-id": token})
                    self._base = base
                    print(f"[vCenter] Autenticado em {base}")
                    return True
            except Exception:
                pass
        print("[vCenter][ERRO] Falha no login.")
        return False

    def get(self, path):
        try:
            r = self.s.get(f"{self._base}/{path}", timeout=15)
            r.raise_for_status()
            raw = r.json()
            return raw.get("value", raw) if isinstance(raw, dict) and "value" in raw else raw
        except Exception as e:
            print(f"  [AVISO] GET {path}: {e}")
            return None

    def post(self, path, body):
        try:
            r = self.s.post(f"{self._base}/{path}", json=body, timeout=30)
            r.raise_for_status()
            raw = r.json()
            return raw.get("value", raw) if isinstance(raw, dict) and "value" in raw else raw
        except Exception as e:
            print(f"  [AVISO] POST {path}: {e}")
            return None

    def logout(self):
        try:
            if self._base:
                path = "session" if "/api" in self._base else "com/vmware/cis/session"
                self.s.delete(f"{self._base}/{path}", timeout=5)
        except Exception:
            pass

    def find_category_id(self, name):
        base_path = "cis/tagging/category" if "/api" in self._base else "com/vmware/cis/tagging/category"
        cat_ids = self.get(base_path) or []
        for cid in cat_ids:
            cat = self.get(f"{base_path}/{cid}")
            if cat and cat.get("name", "").lower() == name.lower():
                return cid
        return None

    def get_tags_in_category(self, category_id):
        """Retorna dict tag_id -> tag_name para a categoria."""
        base_path = "cis/tagging/tag" if "/api" in self._base else "com/vmware/cis/tagging/tag"
        all_ids = self.get(base_path) or []
        result = {}
        for tid in all_ids:
            tag = self.get(f"{base_path}/{tid}")
            if tag and tag.get("category_id") == category_id:
                result[tid] = tag.get("name", "?")
        return result

    def get_vms_by_tag(self, tag_id):
        """Retorna lista de MoRef IDs de VMs com a tag."""
        rest_base = f"https://{VCENTER_HOST}/rest"
        try:
            r = self.s.post(
                f"{rest_base}/com/vmware/cis/tagging/tag-association"
                "?~action=list-attached-objects",
                json={"tag_id": tag_id},
                timeout=30,
            )
            r.raise_for_status()
            objects = r.json().get("value", [])
            return [o["id"] for o in objects if o.get("type") == "VirtualMachine"]
        except Exception as e:
            print(f"  [AVISO] tag-association list-attached-objects: {e}")
            return []


# ─── pyVmomi — métricas das VMs ───────────────────────────────────────────────

def vcenter_connect():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=VCENTER_HOST, user=VCENTER_USER,
        pwd=VCENTER_PASSWORD, port=VCENTER_PORT,
        sslContext=ctx,
    )


def build_vm_metrics_map(si):
    """Retorna dict moref -> métricas para todas as VMs (exceto templates)."""
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    vms = list(container.view)
    container.Destroy()
    print(f"[vCenter] VMs no inventário: {len(vms)}")

    vm_map = {}
    for vm in vms:
        if not vm.config or vm.config.template:
            continue
        moref   = vm._moId
        vcpus   = vm.config.hardware.numCPU or 0
        mem_gb  = round((vm.config.hardware.memoryMB or 0) / 1024, 4)
        prov_gb = used_gb = 0.0
        if vm.summary and vm.summary.storage:
            prov_gb = round(
                (vm.summary.storage.committed + vm.summary.storage.uncommitted) / (1024 ** 3), 4
            )
            used_gb = round(vm.summary.storage.committed / (1024 ** 3), 4)
        vm_map[moref] = {
            "name": vm.config.name,
            "vcpus": vcpus,
            "mem_gb": mem_gb,
            "disk_provisioned_gb": prov_gb,
            "disk_used_gb": used_gb,
        }
    return vm_map


# ─── Coleta principal ─────────────────────────────────────────────────────────

def collect_by_orgao_tag():
    """Retorna dict orgao -> list[moref]."""
    api = VCenterTagAPI()
    if not api.login():
        return {}
    try:
        print(f"[vCenter] Buscando categoria '{TAG_CATEGORY_NAME}'...")
        cat_id = api.find_category_id(TAG_CATEGORY_NAME)
        if not cat_id:
            print(f"[vCenter][ERRO] Categoria '{TAG_CATEGORY_NAME}' não encontrada.")
            return {}

        tags = api.get_tags_in_category(cat_id)
        print(f"[vCenter] Tags em '{TAG_CATEGORY_NAME}': {list(tags.values())}")
        if not tags:
            return {}

        orgao_morefs = {}
        for tid, nome in tags.items():
            morefs = api.get_vms_by_tag(tid)
            orgao_morefs[nome] = morefs
            print(f"  '{nome}': {len(morefs)} VMs")

        if not any(orgao_morefs.values()):
            print("[vCenter][AVISO] Nenhuma VM retornada pelas tags.")
            print("  Verifique o privilégio: Tagging > vSphere Tagging > Get Attached Tags on Object")
            return {}

        return orgao_morefs
    finally:
        api.logout()


def aggregate_metrics(orgao_morefs, vm_map):
    """Cruza MoRefs da Tag API com métricas do pyVmomi."""
    data = defaultdict(lambda: {
        "vms": 0, "vcpus": 0, "mem_gb": 0.0,
        "disk_provisioned_gb": 0.0, "disk_used_gb": 0.0,
    })
    for orgao, morefs in orgao_morefs.items():
        for moref in morefs:
            m = vm_map.get(moref)
            if not m:
                continue
            data[orgao]["vms"]               += 1
            data[orgao]["vcpus"]             += m["vcpus"]
            data[orgao]["mem_gb"]            += m["mem_gb"]
            data[orgao]["disk_provisioned_gb"] += m["disk_provisioned_gb"]
            data[orgao]["disk_used_gb"]      += m["disk_used_gb"]
    return data


# ─── PostgreSQL ───────────────────────────────────────────────────────────────

def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS tag_vcenter (id SERIAL PRIMARY KEY)")
    conn.commit()

    needed = {
        "datetime":             "TIMESTAMPTZ",
        "orgao":                "TEXT",
        "vms":                  "INT",
        "vcpus":                "INT",
        "mem_gb":               "DOUBLE PRECISION",
        "disk_provisioned_gb":  "DOUBLE PRECISION",
        "disk_used_gb":         "DOUBLE PRECISION",
    }
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='tag_vcenter'")
    existing = {r[0] for r in cur.fetchall()}
    for col, typ in needed.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE tag_vcenter ADD COLUMN {col} {typ}")
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
    INSERT INTO tag_vcenter (
        datetime, orgao, vms, vcpus,
        mem_gb, disk_provisioned_gb, disk_used_gb
    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
    """
    for orgao, v in data.items():
        cur.execute(q, (
            now, orgao, v["vms"], v["vcpus"],
            round(v["mem_gb"], 4),
            round(v["disk_provisioned_gb"], 4),
            round(v["disk_used_gb"], 4),
        ))
        print(f"  {orgao:20s}: {v['vms']} VMs | {v['vcpus']} vCPUs | "
              f"{v['mem_gb']:.2f} GB RAM | "
              f"Disco prov: {v['disk_provisioned_gb']:.2f} GB | "
              f"Disco usado: {v['disk_used_gb']:.2f} GB")

    conn.commit()
    cur.close()
    conn.close()
    print(f"[vCenter] {len(data)} órgãos salvos em tag_vcenter.")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Coleta vCenter (categoria Orgao) ===")

    orgao_morefs = collect_by_orgao_tag()
    if not orgao_morefs:
        print("[vCenter] Nada a salvar.")
        sys.exit(0)

    si = vcenter_connect()
    try:
        vm_map = build_vm_metrics_map(si)
    finally:
        Disconnect(si)

    data = aggregate_metrics(orgao_morefs, vm_map)
    if data:
        save(data)
    else:
        print("[vCenter] Nenhuma VM com métricas encontrada.")

    print("[vCenter] Concluído.")
