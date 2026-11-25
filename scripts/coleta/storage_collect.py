"""
Coleta diária dos storages (CronJob capacityplan-storage-collect, 07:00).

Lê direto dos equipamentos — não do que o vCenter apresenta — e grava um
retrato por dia em storage_dc:

  escopo = array   um por equipamento (NetApp, Pure)
  escopo = tier    NetApp por classe de disco (SSD rápido / NL-SAS lento)
  escopo = volume  cada volume/LUN, com o ambiente a que pertence e se é um
                   datastore do vCenter da entidade (cruzamento pelo NAA)

e em storage_cliente_dc quanto cada cliente ocupa em cada datastore do vCenter: o uso de cada VM no
datastore (storage.perDatastoreUsage = soma dos discos/arquivos dela ali; committed = usado,
+ uncommitted = provisionado) vai para o órgão da tag da categoria Orgao da VM. VM sem a tag: a pasta
de órgão do vSphere (origem = 'pasta'), para aparecer e ser corrigida. VM com 2 tags de órgão: vale a
que não é DESATIVADAS. No vSAN o committed já inclui a cópia do RAID, como o usado do datastore.
usado_gib é a parte do cliente no usado do datastore: quando as VMs somam mais que o usado (vSAN com
deduplicação/compressão, ex.: um datastore com 412 TiB nas VMs × 365 TiB no datastore), o usado do datastore é
repartido na proporção de cada cliente; usado_vms_gib guarda o que as VMs informam. Quando somam menos,
a linha cliente = "(fora das VMs)" é o resto (ISOs, arquivos soltos, metadados): a soma sempre bate.

Regras combinadas com a equipe de storage:
  útil         = bruto − RAID (o NetApp já informa no tamanho dos agregados)
  provisionado = todos os volumes (inclui backup/Veeam e OpenShift)
  livre        = útil − provisionado (thin conta como usado; sem dedup/compress)

Variáveis (secret capacityplan-storage-config; equipamento sem host é pulado):
  NETAPP_HOST, NETAPP_USER, NETAPP_PASSWORD
  PURE_HOST, PURE_USER, PURE_PASSWORD (ou PURE_API_TOKEN)
  VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD
  NUTANIX_HOST_1, NUTANIX_USER_1, NUTANIX_PASSWORD_1, NUTANIX_HOST_2, ...  (VMs do Nutanix, ver nutanix_vms.py)
"""
import base64
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2.extras

from .db import get_conn

GiB = 1024 ** 3
AMBIENTE_PROPRIO = os.environ.get("AMBIENTE_PROPRIO", "proprio")   # rótulo do ambiente da própria entidade
NAA_NETAPP, NAA_PURE = "naa.600a0980", "naa.624a9370"
CLASSE = {"solid_state": "SSD", "capacity": "NL-SAS", "performance": "SAS"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_dc (
    id                        BIGSERIAL PRIMARY KEY,
    data_coleta               TIMESTAMPTZ NOT NULL,
    fabricante                VARCHAR(16)  NOT NULL,   -- netapp | pure | vsan
    equipamento               VARCHAR(64)  NOT NULL,
    escopo                    VARCHAR(16)  NOT NULL,   -- array | tier | volume
    tier                      VARCHAR(16),             -- SSD | NL-SAS | NVMe | vSAN
    nome                      VARCHAR(255) NOT NULL,
    ambiente                  VARCHAR(64),             -- ambiente próprio, órgão, Backup (Veeam), OpenShift...
    conectado_a               TEXT,                    -- igroups / host-groups / SVM
    no_vcenter                BOOLEAN,                 -- é datastore do vCenter da entidade
    datastore_vcenter         VARCHAR(255),
    -- visão do storage (GiB)
    bruto_gib                 NUMERIC(16,3),
    util_gib                  NUMERIC(16,3),
    provisionado_gib          NUMERIC(16,3),
    usado_gib                 NUMERIC(16,3),           -- lógico (o que os hosts escreveram)
    fisico_gib                NUMERIC(16,3),           -- ocupado em disco após redução
    snapshots_gib             NUMERIC(16,3),
    livre_gib                 NUMERIC(16,3),           -- útil − provisionado
    reducao_dados             NUMERIC(8,2),
    -- visão do vCenter para o mesmo LUN (GiB)
    vcenter_capacidade_gib    NUMERIC(16,3),
    vcenter_usado_gib         NUMERIC(16,3),
    vcenter_provisionado_gib  NUMERIC(16,3),
    detalhes                  JSONB
);
CREATE INDEX IF NOT EXISTS idx_storage_dc_data ON storage_dc (data_coleta DESC, fabricante, escopo);
CREATE TABLE IF NOT EXISTS storage_cliente_dc (
    data_coleta               TIMESTAMPTZ  NOT NULL,
    datastore                 VARCHAR(255) NOT NULL,   -- nome no vCenter (= storage_dc.datastore_vcenter)
    cliente                   VARCHAR(255) NOT NULL,   -- tag Orgao da VM (ou a pasta, se não tem a tag)
    origem                    VARCHAR(9)   NOT NULL,   -- tag | pasta | datastore (linha "fora das VMs")
    vms                       INTEGER      NOT NULL,
    usado_gib                 NUMERIC(16,3),           -- parte do cliente no usado do datastore
    usado_vms_gib             NUMERIC(16,3),           -- committed das VMs (antes de dedup/compressão)
    provisionado_gib          NUMERIC(16,3),
    PRIMARY KEY (data_coleta, datastore, cliente, origem)
);
"""
FORA_DAS_VMS, SEM_PASTA, CATEGORIA_ORGAO = "(fora das VMs)", "(sem pasta)", "Orgao"


def _gib(b):
    return round(b / GiB, 3) if b is not None else None


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
    return c


def _http(url, metodo="GET", corpo=None, headers=None, cabecalhos=False):
    req = urllib.request.Request(url, method=metodo, headers={"Accept": "application/json", **(headers or {})},
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    if corpo is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ctx(), timeout=90) as r:
        dados = json.loads(r.read() or b"{}")
        return (dados, {k.lower(): v for k, v in r.headers.items()}) if cabecalhos else dados


# ───────────────────────── vCenter (cruza os LUNs e reparte por cliente) ─────────────────────────
def _tags_orgao(host):
    """{moId da VM: órgão} pela categoria de tags Orgao (API REST do vCenter; o pyVmomi não lê tags)."""
    auth = base64.b64encode(f"{os.environ['VCENTER_USER']}:{os.environ['VCENTER_PASSWORD']}".encode()).decode()
    base = f"https://{host}/api"
    tok = _http(f"{base}/session", "POST", headers={"Authorization": f"Basic {auth}"})
    h = {"vmware-api-session-id": tok}
    try:
        cat = next(c for c in _http(f"{base}/cis/tagging/category", headers=h)
                   if _http(f"{base}/cis/tagging/category/{c}", headers=h)["name"] == CATEGORIA_ORGAO)
        ids = _http(f"{base}/cis/tagging/tag?action=list-tags-for-category", "POST", {"category_id": cat}, headers=h)
        nome = {t: _http(f"{base}/cis/tagging/tag/{t}", headers=h)["name"] for t in ids}
        por_vm = defaultdict(set)
        for r in _http(f"{base}/cis/tagging/tag-association?action=list-attached-objects-on-tags", "POST",
                       {"tag_ids": ids}, headers=h):
            for o in r["object_ids"]:
                if o["type"] == "VirtualMachine":
                    por_vm[o["id"]].add(nome[r["tag_id"]])
    finally:
        try:
            _http(f"{base}/session", "DELETE", headers=h)
        except Exception:
            pass
    # 2 tags de órgão (ex.: CAGEPA + DESATIVADAS): vale a que não é DESATIVADAS
    return {vm: sorted(t, key=lambda x: (x.upper().startswith("DESATIVADA"), x))[0] for vm, t in por_vm.items()}


def _por_cliente(si, vim, tags):
    """[{datastore, cliente, origem, vms, usado_gib, provisionado_gib}]: uso das VMs por datastore e órgão (tag
    Orgao; sem a tag, a pasta de 1º nível abaixo da pasta de VMs), mais a linha FORA_DAS_VMS de cada datastore."""
    from pyVmomi import vmodl
    pc, ct = si.content.propertyCollector, si.content
    view = ct.viewManager.CreateContainerView(ct.rootFolder, [vim.VirtualMachine, vim.Folder, vim.Datastore], True)
    P = vmodl.query.PropertyCollector
    spec = P.FilterSpec(
        objectSet=[P.ObjectSpec(obj=view, skip=True, selectSet=[P.TraversalSpec(name="v", path="view", skip=False,
                                                                                type=vim.view.ContainerView)])],
        propSet=[P.PropertySpec(type=vim.VirtualMachine, pathSet=["parent", "storage.perDatastoreUsage"]),
                 P.PropertySpec(type=vim.Folder, pathSet=["name", "parent"]),
                 P.PropertySpec(type=vim.Datastore, pathSet=["name", "summary.capacity", "summary.freeSpace"])])
    pastas, vms, dss = {}, [], {}
    for o in pc.RetrieveContents([spec]):
        props = {p.name: p.val for p in o.propSet}
        if isinstance(o.obj, vim.Folder):
            pastas[o.obj._moId] = (props.get("name"), props.get("parent"))
        elif isinstance(o.obj, vim.Datastore):
            dss[o.obj._moId] = (props["name"], props["summary.capacity"] - props["summary.freeSpace"])
        else:
            vms.append({**props, "moid": o.obj._moId})
    view.Destroy()

    def pasta_de(pai):
        # sobe até a pasta cujo pai é a pasta de VMs do datacenter (o pai dela é o Datacenter)
        while pai is not None and pai._moId in pastas:
            nome, avo = pastas[pai._moId]
            if avo is None or isinstance(avo, vim.Datacenter):
                return SEM_PASTA                        # a VM está na raiz da pasta de VMs
            if avo._moId in pastas and isinstance(pastas[avo._moId][1], vim.Datacenter):
                return nome
            pai = avo
        return SEM_PASTA

    soma = defaultdict(lambda: [0, 0, 0])               # (datastore, cliente, origem) -> vms, committed, uncommitted
    for vm in vms:
        cliente, origem = (tags[vm["moid"]], "tag") if vm["moid"] in tags else (pasta_de(vm.get("parent")), "pasta")
        for u in vm.get("storage.perDatastoreUsage") or []:
            if u.datastore._moId in dss:
                s = soma[(dss[u.datastore._moId][0], cliente, origem)]
                s[0] += 1
                s[1] += u.committed or 0
                s[2] += u.uncommitted or 0
    das_vms = defaultdict(int)
    for (ds, _, _), (_, c, _) in soma.items():
        das_vms[ds] += c
    usado_ds = {nome: usado for nome, usado in dss.values()}
    fator = {ds: min(1.0, usado_ds.get(ds, 0) / tot) if tot else 1.0 for ds, tot in das_vms.items()}
    saida = [{"datastore": ds, "cliente": cli, "origem": org, "vms": n, "usado_gib": _gib(c * fator[ds]),
              "usado_vms_gib": _gib(c), "provisionado_gib": _gib(c + unc)}
             for (ds, cli, org), (n, c, unc) in soma.items()]
    for nome, usado in dss.values():
        fora = usado - das_vms.get(nome, 0)
        if fora > 0:
            saida.append({"datastore": nome, "cliente": FORA_DAS_VMS, "origem": "datastore", "vms": 0,
                          "usado_gib": _gib(fora), "usado_vms_gib": None, "provisionado_gib": _gib(fora)})
    return saida



def _vcenter():
    if not os.environ.get("VCENTER_HOST"):
        return {}
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
    host = os.environ["VCENTER_HOST"].split("://")[-1].strip("/")
    si = SmartConnect(host=host, user=os.environ["VCENTER_USER"],
                      pwd=os.environ["VCENTER_PASSWORD"], sslContext=_ctx())
    try:
        view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vim.Datastore], True)
        por_naa, vsan = {}, []
        for ds in view.view:
            s = ds.summary
            usado = s.capacity - s.freeSpace
            item = {"nome": s.name, "cap": s.capacity, "usado": usado,
                    "prov": usado + (getattr(s, "uncommitted", 0) or 0), "vms": len(ds.vm or [])}
            if s.type == "vsan":
                hosts = [m.key for m in (ds.host or [])]
                item["cluster"] = hosts[0].parent.name if hosts and hosts[0].parent else None
                vsan.append(item)
            elif isinstance(ds.info, vim.host.VmfsDatastoreInfo):
                for e in ds.info.vmfs.extent:
                    por_naa[e.diskName.lower()] = item
        try:
            tags = _tags_orgao(host)
        except Exception as e:      # sem as tags, reparte pelas pastas
            print(f"[storage] tags do vCenter: ERRO {e} (repartindo pelas pastas)")
            tags = {}
        return {"por_naa": por_naa, "vsan": vsan, "clientes": _por_cliente(si, vim, tags)}
    finally:
        Disconnect(si)


# ───────────────────────── NetApp ─────────────────────────
def _ambiente_netapp(vol, svm):
    n, s = vol.upper(), (svm or "").lower()
    if "openshift" in s:
        return "OpenShift"
    if n.startswith("VEEAM"):
        return "Backup (Veeam)"
    m = re.match(r"^([A-Z0-9]+)_NETAPP", n)
    if m:
        return m.group(1)
    if s.startswith("svm_nfs"):
        return "NFS"
    if s.startswith("svm_cifs"):
        return "CIFS"
    return "Outros"


def _netapp(vc):
    host = os.environ["NETAPP_HOST"].split("://")[-1].strip("/")
    auth = "Basic " + base64.b64encode(f"{os.environ['NETAPP_USER']}:{os.environ['NETAPP_PASSWORD']}".encode()).decode()

    def get(caminho, **params):
        url = f"https://{host}/api{caminho}?" + urllib.parse.urlencode(params)
        out = []
        while url:
            d = _http(url, headers={"Authorization": auth})
            if "records" not in d:
                return d
            out += d["records"]
            prox = d.get("_links", {}).get("next", {}).get("href")
            url = f"https://{host}{prox}" if prox else None
        return out

    cluster = get("/cluster", fields="name,version")
    nodes = get("/cluster/nodes", fields="name,model,serial_number")
    discos = get("/storage/disks", fields="class,physical_size,container_type")
    ags = get("/storage/aggregates", fields="name,block_storage.primary.disk_class,block_storage.primary.raid_type,space")
    vols = get("/storage/volumes", fields="name,svm.name,aggregates.name,space.size,space.used,is_svm_root,guarantee.type")
    luns = get("/storage/luns", fields="name,serial_number,location.volume.name,space.size,space.used,os_type")
    maps = get("/protocols/san/lun-maps", fields="lun.name,igroup.name")

    classe_ag = {a["name"]: CLASSE.get(a.get("block_storage", {}).get("primary", {}).get("disk_class"), "?") for a in ags}
    igroups = defaultdict(set)
    for m in maps:
        igroups[m["lun"]["name"]].add(m["igroup"]["name"])
    luns_por_vol = defaultdict(list)
    for l in luns:
        luns_por_vol[l.get("location", {}).get("volume", {}).get("name")].append(l)

    nome = cluster.get("name", "netapp")
    base = {"fabricante": "netapp", "equipamento": nome}
    linhas, tier = [], defaultdict(lambda: defaultdict(float))

    for k in discos:
        t = tier[CLASSE.get(k.get("class"), "?")]
        t["bruto"] += k.get("physical_size") or 0
        t["discos"] += 1
        t["spares"] += 1 if k.get("container_type") == "spare" else 0
    for a in ags:
        t = tier[classe_ag[a["name"]]]
        sp = a.get("space", {})
        t["util"] += sp.get("block_storage", {}).get("size") or 0
        t["fisico"] += sp.get("block_storage", {}).get("used") or 0
        t["efic_logico"] += (sp.get("efficiency", {}).get("logical_used") or 0)

    for v in vols:
        if v.get("is_svm_root"):
            continue
        cl = classe_ag.get((v.get("aggregates") or [{}])[0].get("name"), "?")
        svm = v.get("svm", {}).get("name")
        vsp = v.get("space", {})
        t = tier[cl]
        t["prov"] += vsp.get("size") or 0
        t["usado"] += vsp.get("used") or 0
        t["volumes"] += 1

        lns = luns_por_vol.get(v["name"], [])
        naas = [NAA_NETAPP + (l.get("serial_number") or "").encode().hex() for l in lns]
        ds = next((vc.get("por_naa", {}).get(n) for n in naas if n in vc.get("por_naa", {})), None)
        linhas.append({**base, "escopo": "volume", "tier": cl, "nome": v["name"],
                       "ambiente": _ambiente_netapp(v["name"], svm),
                       "conectado_a": ", ".join(sorted(set().union(*[igroups[l["name"]] for l in lns]))) or svm,
                       "no_vcenter": ds is not None, "datastore_vcenter": ds and ds["nome"],
                       "provisionado_gib": _gib(vsp.get("size")), "usado_gib": _gib(vsp.get("used")),
                       "vcenter_capacidade_gib": ds and _gib(ds["cap"]), "vcenter_usado_gib": ds and _gib(ds["usado"]),
                       "vcenter_provisionado_gib": ds and _gib(ds["prov"]),
                       "detalhes": {"svm": svm, "thin": (v.get("guarantee") or {}).get("type") == "none",
                                    "luns": [{"nome": l["name"], "tamanho_gib": _gib(l.get("space", {}).get("size")),
                                              "os": l.get("os_type")} for l in lns],
                                    "vms": ds and ds["vms"]}})

    for cl, t in tier.items():
        if cl == "?":
            continue
        linhas.append({**base, "escopo": "tier", "tier": cl, "nome": cl,
                       "bruto_gib": _gib(t["bruto"]), "util_gib": _gib(t["util"]), "provisionado_gib": _gib(t["prov"]),
                       "usado_gib": _gib(t["usado"]), "fisico_gib": _gib(t["fisico"]),
                       "livre_gib": _gib(t["util"] - t["prov"]),
                       "reducao_dados": round(t["efic_logico"] / t["fisico"], 2) if t["fisico"] else None,
                       "detalhes": {"discos": int(t["discos"]), "spares": int(t["spares"]), "volumes": int(t["volumes"]),
                                    "agregados": [{"nome": a["name"], "raid": a.get("block_storage", {}).get("primary", {}).get("raid_type"),
                                                   "tamanho_gib": _gib(a.get("space", {}).get("block_storage", {}).get("size")),
                                                   "usado_gib": _gib(a.get("space", {}).get("block_storage", {}).get("used"))}
                                                  for a in ags if classe_ag[a["name"]] == cl]}})

    tot = {k: sum(t[k] for c, t in tier.items() if c != "?") for k in ("bruto", "util", "prov", "usado", "fisico")}
    linhas.append({**base, "escopo": "array", "nome": nome,
                   "bruto_gib": _gib(tot["bruto"]), "util_gib": _gib(tot["util"]), "provisionado_gib": _gib(tot["prov"]),
                   "usado_gib": _gib(tot["usado"]), "fisico_gib": _gib(tot["fisico"]),
                   "livre_gib": _gib(tot["util"] - tot["prov"]),
                   "detalhes": {"versao": cluster.get("version", {}).get("full"),
                                "nodes": [{"nome": n["name"], "modelo": n.get("model"), "serie": n.get("serial_number")} for n in nodes],
                                "luns": len(luns), "volumes": sum(1 for v in vols if not v.get("is_svm_root"))}})
    return linhas


# ───────────────────────── Pure Storage ─────────────────────────
def _pure(vc):
    host = os.environ["PURE_HOST"].split("://")[-1].strip("/")
    base_url = f"https://{host}"
    versoes = _http(f"{base_url}/api/api_version")["version"]
    chave = lambda v: tuple(map(int, v.split(".")))
    v1 = max((v for v in versoes if v.startswith("1.")), key=chave)
    v2 = max((v for v in versoes if v.startswith("2.")), key=chave)
    token = os.environ.get("PURE_API_TOKEN") or _http(
        f"{base_url}/api/{v1}/auth/apitoken", "POST",
        {"username": os.environ["PURE_USER"], "password": os.environ["PURE_PASSWORD"]})["api_token"]
    _, hdr = _http(f"{base_url}/api/{v2}/login", "POST", headers={"api-token": token}, cabecalhos=True)
    sessao = {"x-auth-token": hdr["x-auth-token"]}

    def get(recurso, **params):
        out, cont = [], None
        while True:
            q = urllib.parse.urlencode({**params, **({"continuation_token": cont} if cont else {})})
            d = _http(f"{base_url}/api/{v2}/{recurso}?{q}", headers=sessao)
            out += d.get("items", [])
            cont = d.get("continuation_token")
            if not cont:
                return out

    arr = (get("arrays") or [{}])[0]
    esp = (get("arrays/space") or [{}])[0]
    perf = (get("arrays/performance") or [{}])[0]
    ctrl = get("controllers")
    drives = get("drives")
    vols = get("volumes", destroyed="false")
    conex = get("connections")
    snaps = get("volume-snapshots", destroyed="false")
    pgs = get("protection-groups")
    alertas = get("alerts", filter="state='open'")

    nome = arr.get("name", "pure")
    base = {"fabricante": "pure", "equipamento": nome, "tier": "NVMe"}
    destino = defaultdict(set)
    for c in conex:
        destino[c["volume"]["name"]].add((c.get("host_group") or {}).get("name") or (c.get("host") or {}).get("name"))

    linhas = []
    for v in vols:
        vs = v.get("space", {})
        naa = NAA_PURE + (v.get("serial") or "").lower()
        ds = vc.get("por_naa", {}).get(naa)
        grupos = sorted(destino.get(v["name"], []))
        ambiente = grupos[0].split("-HG")[0].split("_HG")[0] if grupos else "Sem conexão"
        linhas.append({**base, "escopo": "volume", "nome": v["name"], "ambiente": ambiente,
                       "conectado_a": ", ".join(grupos), "no_vcenter": ds is not None,
                       "datastore_vcenter": ds and ds["nome"],
                       "provisionado_gib": _gib(v.get("provisioned")), "usado_gib": _gib(vs.get("virtual")),
                       "fisico_gib": _gib(vs.get("total_physical")), "snapshots_gib": _gib(vs.get("snapshots")),
                       "reducao_dados": vs.get("data_reduction"),
                       "vcenter_capacidade_gib": ds and _gib(ds["cap"]), "vcenter_usado_gib": ds and _gib(ds["usado"]),
                       "vcenter_provisionado_gib": ds and _gib(ds["prov"]),
                       "detalhes": {"vms": ds and ds["vms"], "serial": v.get("serial")}})

    sp = esp.get("space", {})
    cap = esp.get("capacity") or 0
    linhas.append({**base, "escopo": "array", "nome": nome,
                   "bruto_gib": _gib(sum(d.get("capacity") or 0 for d in drives)), "util_gib": _gib(cap),
                   "provisionado_gib": _gib(sp.get("total_provisioned")), "usado_gib": _gib(sp.get("virtual")),
                   "fisico_gib": _gib(sp.get("total_physical")), "snapshots_gib": _gib(sp.get("snapshots")),
                   "livre_gib": _gib(cap - (sp.get("total_provisioned") or 0)),
                   "reducao_dados": sp.get("data_reduction"),
                   "detalhes": {
                       "versao": f"{arr.get('os')} {arr.get('version')}",
                       "livre_fisico_gib": _gib(cap - (sp.get("total_physical") or 0)),
                       "reducao_total": sp.get("total_reduction"), "thin_provisioning": sp.get("thin_provisioning"),
                       "unicos_gib": _gib(sp.get("unique")), "compartilhado_gib": _gib(sp.get("shared")),
                       "controladoras": [{"nome": c.get("name"), "modelo": c.get("model"), "modo": c.get("mode"),
                                          "status": c.get("status")} for c in ctrl],
                       "drives": {"ssd": sum(1 for d in drives if d.get("type") not in (None, "-") and d.get("status") == "healthy"),
                                  "slots_livres": sum(1 for d in drives if d.get("status") == "unused"),
                                  "com_problema": sum(1 for d in drives if d.get("status") not in ("healthy", "unused"))},
                       "desempenho": {"iops_leitura": perf.get("reads_per_sec"), "iops_escrita": perf.get("writes_per_sec"),
                                      "mbps_leitura": round((perf.get("read_bytes_per_sec") or 0) / 1e6, 1),
                                      "mbps_escrita": round((perf.get("write_bytes_per_sec") or 0) / 1e6, 1),
                                      "latencia_leitura_ms": round((perf.get("usec_per_read_op") or 0) / 1000, 3),
                                      "latencia_escrita_ms": round((perf.get("usec_per_write_op") or 0) / 1000, 3)},
                       "protecao": {"snapshots": len(snaps), "grupos": [
                           {"nome": p.get("name"),
                            "snapshots": bool((p.get("snapshot_schedule") or {}).get("enabled")),
                            "replicacao": bool((p.get("replication_schedule") or {}).get("enabled"))} for p in pgs]},
                       "alertas": [{"severidade": a.get("severity"), "resumo": a.get("summary")} for a in alertas]}})
    return linhas


# ───────────────────────── vSAN (como o vCenter apresenta) ─────────────────────────
def _vsan_cluster(vc):
    """
    Totais do cluster vSAN gravados pelo datacenter_collector (API do vSAN):
    útil = resiliente = bruto − host rebuild reserve − operations reserve.
    O usado já inclui as cópias do FTT (RAID-5), por isso não se divide o útil.

    Por cluster (um datastore vSAN por cluster): bruto = capacidade do datastore,
    reservas = total − resiliente da linha CLUSTER da tabela datacenter (só o vSAN
    tem reserva), usado = usado do datastore no vCenter (o mesmo "Used" da tela
    Monitor > vSAN > Capacity de cada cluster). O total do vSAN é a soma dos clusters.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT total_gb, resilient_gb, used_gb, provisioned_gb,
                       host_rebuild_reserve_gb, operations_reserve_gb, data_coleta
                FROM datacenter_storage_vendor
                WHERE upper(vendor) = 'VSAN' AND scope = 'PLATFORM' AND plataforma = 'vmware'
                ORDER BY data_coleta DESC LIMIT 1
            """)
            r = cur.fetchone()
            cur.execute("""
                SELECT cluster, storage_fast_total_gb - storage_fast_resilient_gb
                FROM datacenter
                WHERE plataforma = 'vmware' AND scope = 'CLUSTER'
                  AND data_coleta = (SELECT max(data_coleta) FROM datacenter
                                     WHERE plataforma = 'vmware' AND scope = 'CLUSTER')
            """)
            reservas = {c: float(v or 0) for c, v in cur.fetchall()}
    if not r or not r[0]:
        return []
    f = lambda v: None if v is None else float(v)
    bruto, util, usado, prov, rebuild, oper, coleta = (*map(f, r[:6]), r[6])

    clusters = []
    for d in (d for d in vc.get("vsan", []) if d.get("cluster")):
        cap = _gib(d["cap"])
        res = reservas.get(d["cluster"], 0.0)
        u = _gib(d["usado"])
        clusters.append({"fabricante": "vsan", "equipamento": "VxRail", "escopo": "cluster", "tier": "vSAN",
                         "nome": d["cluster"], "ambiente": AMBIENTE_PROPRIO, "datastore_vcenter": d["nome"],
                         "bruto_gib": cap, "util_gib": cap - res, "usado_gib": u, "fisico_gib": u,
                         "livre_gib": cap - res - u,
                         "vcenter_capacidade_gib": cap, "vcenter_usado_gib": _gib(d["usado"]),
                         "vcenter_provisionado_gib": _gib(d["prov"]),
                         "detalhes": {"reservas_gib": res, "vms": d["vms"]}})
    if clusters:  # total = soma dos clusters, igual ao que o vSphere mostra por cluster
        bruto = sum(c["bruto_gib"] for c in clusters)
        util = sum(c["util_gib"] for c in clusters)
        usado = sum(c["usado_gib"] for c in clusters)
    return clusters + [{"fabricante": "vsan", "equipamento": "VxRail", "escopo": "array", "tier": "vSAN",
             "nome": "vSAN (VxRail)", "ambiente": AMBIENTE_PROPRIO, "bruto_gib": bruto, "util_gib": util,
             "usado_gib": usado, "fisico_gib": usado, "provisionado_gib": prov,
             "livre_gib": util - usado if util is not None and usado is not None else None,
             "detalhes": {"reserva_rebuild_gib": rebuild, "reserva_operations_gib": oper,
                          "fonte": "soma dos clusters (vCenter)" if clusters else "datacenter_storage_vendor", "coleta_fonte": coleta.isoformat()}}]


def _vsan(vc):
    return [{"fabricante": "vsan", "equipamento": "VxRail", "escopo": "volume", "tier": "vSAN",
             "nome": d["nome"], "ambiente": AMBIENTE_PROPRIO, "no_vcenter": True, "datastore_vcenter": d["nome"],
             "vcenter_capacidade_gib": _gib(d["cap"]), "vcenter_usado_gib": _gib(d["usado"]),
             "vcenter_provisionado_gib": _gib(d["prov"]), "detalhes": {"vms": d["vms"]}}
            for d in vc.get("vsan", [])]


COLUNAS = ["fabricante", "equipamento", "escopo", "tier", "nome", "ambiente", "conectado_a", "no_vcenter",
           "datastore_vcenter", "bruto_gib", "util_gib", "provisionado_gib", "usado_gib", "fisico_gib",
           "snapshots_gib", "livre_gib", "reducao_dados", "vcenter_capacidade_gib", "vcenter_usado_gib",
           "vcenter_provisionado_gib", "detalhes"]


def run():
    agora = datetime.now(timezone.utc)
    linhas, erros = [], []
    try:
        vc = _vcenter()
    except Exception as e:  # sem vCenter a coleta segue, só sem marcar os datastores
        vc = {}
        erros.append(f"vcenter: {type(e).__name__}: {e}")
        print(f"[storage] vcenter: ERRO {e} (seguindo sem cruzar com os datastores)")
    for nome, coletor, var in (("netapp", _netapp, "NETAPP_HOST"), ("pure", _pure, "PURE_HOST")):
        if not os.environ.get(var):
            print(f"[storage] {nome}: sem {var}, pulado")
            continue
        try:
            novas = coletor(vc)
            linhas += novas
            print(f"[storage] {nome}: {len(novas)} linhas")
        except Exception as e:  # um equipamento fora do ar não impede o outro
            erros.append(f"{nome}: {type(e).__name__}: {e}")
            print(f"[storage] {nome}: ERRO {e}")
    linhas += _vsan(vc)
    try:
        linhas += _vsan_cluster(vc)
    except Exception as e:
        erros.append(f"vsan: {type(e).__name__}: {e}")
        print(f"[storage] vsan cluster: ERRO {e}")
    from . import nutanix_vms    # importado aqui: o módulo usa _http/GiB deste
    if nutanix_vms.clusters():
        # Nutanix fora do alcance só gera aviso: não derruba a coleta dos storages nem faz o
        # Job repetir (a API volta à regra antiga do Nutanix nos dias sem leitura).
        nut, erros_nut = nutanix_vms.coletar()
        for e in erros_nut:
            print(f"[nutanix] AVISO: {e}")
    else:
        nut = []
        print("[nutanix] sem NUTANIX_HOST_1, pulado")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            psycopg2.extras.execute_values(
                cur, f"INSERT INTO storage_dc (data_coleta, {', '.join(COLUNAS)}) VALUES %s",
                [(agora, *[psycopg2.extras.Json(l.get(c)) if c == "detalhes" else l.get(c) for c in COLUNAS])
                 for l in linhas])
            clientes = vc.get("clientes") or []
            if clientes:
                psycopg2.extras.execute_values(
                    cur, "INSERT INTO storage_cliente_dc (data_coleta, datastore, cliente, origem, vms, usado_gib, "
                         "usado_vms_gib, provisionado_gib) VALUES %s",
                    [(agora, c["datastore"], c["cliente"], c["origem"], c["vms"], c["usado_gib"], c["usado_vms_gib"],
                      c["provisionado_gib"]) for c in clientes])
            if nut:
                cur.execute(nutanix_vms.SCHEMA)
                psycopg2.extras.execute_values(
                    cur, f"INSERT INTO nutanix_vms_dc (data_coleta, {', '.join(nutanix_vms.COLUNAS)}) VALUES %s",
                    [(agora, *[n[c] for c in nutanix_vms.COLUNAS]) for n in nut])
        conn.commit()
    print(f"[storage] {len(linhas)} linhas gravadas em storage_dc e {len(vc.get('clientes') or [])} em storage_cliente_dc "
          f"({agora:%Y-%m-%d %H:%M} UTC); {len(nut)} em nutanix_vms_dc")
    if erros:
        raise SystemExit("[storage] falhas: " + " | ".join(erros))


if __name__ == "__main__":
    run()
