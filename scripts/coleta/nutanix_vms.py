"""
VMs do Nutanix por cluster (roda junto da coleta diária dos storages, 07:00).

O coletor do datacenter (tabela datacenter) subconta o Nutanix:
  * vCPU = só num_vcpus (sockets), sem num_cores_per_vcpu;
  * as CVMs (VMs de controle, uma por host) ficam de fora — a API v2 /vms não as lista;
  * disco provisionado = o usado (os containers não têm reserva).
Aqui se lê o Prism Element de cada cluster e grava, em nutanix_vms_dc, uma linha por cluster e
tipo (usuario | cvm) com VMs, vCPU (sockets × cores), memória e disco provisionado (soma dos vDisks,
sem CD-ROM; é tamanho lógico, como o disco lógico = resiliente ÷ RF). A API (capacity.py) usa a
coleta do mesmo dia para corrigir o provisionado de CPU, memória e disco e a contagem de VMs.

Credenciais: NUTANIX_HOST_1, NUTANIX_USER_1, NUTANIX_PASSWORD_1, NUTANIX_HOST_2, ... (o usuário e a
senha caem em NUTANIX_USER / NUTANIX_PASSWORD quando o do cluster não é informado).
"""
import base64
import os
import socket

from .storage_collect import GiB, _http

SCHEMA = """
CREATE TABLE IF NOT EXISTS nutanix_vms_dc (
    data_coleta               TIMESTAMPTZ  NOT NULL,
    cluster                   VARCHAR(255) NOT NULL,   -- nome do cluster no Prism (= datacenter.cluster)
    tipo                      VARCHAR(7)   NOT NULL,   -- usuario | cvm
    vms                       INTEGER      NOT NULL,
    vms_ligadas               INTEGER      NOT NULL,
    vcpu                      INTEGER      NOT NULL,   -- num_vcpus × num_cores_per_vcpu
    memoria_gib               NUMERIC(14,3) NOT NULL,
    disco_provisionado_gib    NUMERIC(16,3),           -- soma dos vDisks (só VMs de usuário)
    ghz_por_core              NUMERIC(8,4),            -- capacidade de CPU do cluster ÷ cores físicos
    PRIMARY KEY (data_coleta, cluster, tipo)
);
ALTER TABLE nutanix_vms_dc ADD COLUMN IF NOT EXISTS vcpu_ligadas INTEGER;   -- vCPUs das VMs ligadas
"""


def clusters():
    """[(host, user, senha)] a partir de NUTANIX_HOST_1, NUTANIX_HOST_2, ..."""
    saida, n = [], 1
    while os.environ.get(f"NUTANIX_HOST_{n}"):
        saida.append((os.environ[f"NUTANIX_HOST_{n}"].split("://")[-1].strip("/").split(":")[0],
                      os.environ.get(f"NUTANIX_USER_{n}") or os.environ.get("NUTANIX_USER", ""),
                      os.environ.get(f"NUTANIX_PASSWORD_{n}") or os.environ.get("NUTANIX_PASSWORD", "")))
        n += 1
    return saida


def _vcpu(v):
    """vCPUs de uma VM (API v2): sockets × núcleos por vCPU."""
    return int(v.get("num_vcpus") or 0) * max(int(v.get("num_cores_per_vcpu") or 1), 1)


def coletar_cluster(host, user, senha):
    """[linha usuario, linha cvm] de um cluster."""
    h = {"Authorization": "Basic " + base64.b64encode(f"{user}:{senha}".encode()).decode()}
    v2 = f"https://{host}:9440/PrismGateway/services/rest/v2.0"
    socket.create_connection((host, 9440), timeout=10).close()   # sem rota/firewall: falha em 10 s, não em 90
    nome = _http(f"{v2}/cluster", headers=h)["name"]
    hosts = _http(f"{v2}/hosts", headers=h).get("entities") or []
    cores = sum(int(x.get("num_cpu_cores") or 0) for x in hosts)
    hz = sum(float(x.get("cpu_capacity_in_hz") or 0) for x in hosts)
    ghz_core = round(hz / 1e9 / cores, 4) if cores else None

    vms = _http(f"{v2}/vms?include_vm_disk_config=true", headers=h).get("entities") or []
    usuario = {
        "cluster": nome, "tipo": "usuario", "vms": len(vms),
        "vms_ligadas": sum(1 for v in vms if str(v.get("power_state", "")).lower() == "on"),
        "vcpu": sum(_vcpu(v) for v in vms),
        "vcpu_ligadas": sum(_vcpu(v) for v in vms if str(v.get("power_state", "")).lower() == "on"),
        "memoria_gib": round(sum(float(v.get("memory_mb") or 0) for v in vms) / 1024, 3),
        "disco_provisionado_gib": round(sum(float(d.get("size") or 0) for v in vms
                                            for d in (v.get("vm_disk_info") or [])
                                            if not d.get("is_cdrom")) / GiB, 3),
        "ghz_por_core": ghz_core,
    }
    # CVMs só aparecem na API v1 (controllerVm = true); numVCpus já é o total de vCPUs.
    cvms = [v for v in (_http(f"https://{host}:9440/PrismGateway/services/rest/v1/vms", headers=h)
                        .get("entities") or []) if v.get("controllerVm")]
    cvm = {
        "cluster": nome, "tipo": "cvm", "vms": len(cvms),
        "vms_ligadas": sum(1 for v in cvms if str(v.get("powerState", "")).lower() == "on"),
        "vcpu": sum(int(v.get("numVCpus") or 0) for v in cvms),
        "vcpu_ligadas": sum(int(v.get("numVCpus") or 0) for v in cvms
                            if str(v.get("powerState", "")).lower() == "on"),
        "memoria_gib": round(sum(float(v.get("memoryCapacityInBytes") or 0) for v in cvms) / GiB, 3),
        "disco_provisionado_gib": None,
        "ghz_por_core": ghz_core,
    }
    return [usuario, cvm]


COLUNAS = ["cluster", "tipo", "vms", "vms_ligadas", "vcpu", "vcpu_ligadas", "memoria_gib", "disco_provisionado_gib",
           "ghz_por_core"]


def coletar():
    """(linhas, erros) de todos os clusters configurados; um cluster fora do ar não impede o outro."""
    linhas, erros = [], []
    for host, user, senha in clusters():
        try:
            novas = coletar_cluster(host, user, senha)
            linhas += novas
            u, c = novas
            print(f"[nutanix] {u['cluster']}: {u['vms']} VMs + {c['vms']} CVMs, {u['vcpu']}+{c['vcpu']} vCPU, "
                  f"{u['memoria_gib'] + c['memoria_gib']:.0f} GiB, disco provisionado {u['disco_provisionado_gib']:.0f} GiB")
        except Exception as e:
            erros.append(f"nutanix {host}: {type(e).__name__}: {e}")
            print(f"[nutanix] {host}: ERRO {e}")
    return linhas, erros
