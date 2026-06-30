import ssl
import atexit
import psycopg2
from pyVim import connect
from pyVmomi import vim
from datetime import datetime, timezone
from env import (
    VCENTER_HOST, VCENTER_USER, VCENTER_PASSWORD,
    POSTGRES_HOST, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB, POSTGRES_PORT
)


def validate_metrics_list(metrics, expected_len):
    """Valida e retorna apenas as linhas com o comprimento esperado."""
    valid = []
    for i, row in enumerate(metrics):
        if not isinstance(row, (list, tuple)):
            print(f"[VALIDATION] Linha {i} não é lista/tuple: {row!r}")
            continue
        if len(row) != expected_len:
            print(f"[VALIDATION] Linha {i} possui {len(row)} colunas (esperado {expected_len}):")
            print(row)
            continue
        valid.append(row)
    return valid


def collect_vcenter_metrics():
    now = datetime.now(timezone.utc)
    metrics = []

    try:
        print("▶ Conectando ao vCenter...")
        ssl_context = ssl._create_unverified_context()
        si = connect.SmartConnect(
            host=VCENTER_HOST, user=VCENTER_USER, pwd=VCENTER_PASSWORD, sslContext=ssl_context
        )
        atexit.register(connect.Disconnect, si)
    except Exception as e:
        print(f"❌ Erro ao conectar ao vCenter: {e}")
        return metrics

    try:
        content = si.RetrieveContent()
    except Exception as e:
        print(f"❌ Erro ao recuperar conteúdo do vCenter: {e}")
        return metrics

    def summarize_entity(entity, level, name, hosts):
        # CPU física (MHz -> transformações e percentuais)
        try:
            cpu_total_mhz = sum(
                (h.hardware.cpuInfo.numCpuCores or 0) * ((h.hardware.cpuInfo.hz or 0) / 1e6)
                for h in hosts
            )
        except Exception:
            cpu_total_mhz = 0

        try:
            cpu_used_mhz = sum(getattr(h.summary.quickStats, 'overallCpuUsage', 0) or 0 for h in hosts)
        except Exception:
            cpu_used_mhz = 0

        cpu_usage_percent = (cpu_used_mhz / cpu_total_mhz * 100) if cpu_total_mhz > 0 else 0

        cpu_total_cores = sum(getattr(h.hardware.cpuInfo, 'numCpuCores', 0) or 0 for h in hosts)
        cpu_total_ghz = cpu_total_mhz / 1000
        cpu_used_ghz = cpu_used_mhz / 1000
        cpu_available_ghz = cpu_total_ghz - cpu_used_ghz
        cpu_used_cores = (cpu_used_ghz / (cpu_total_ghz / cpu_total_cores)) if (cpu_total_ghz > 0 and cpu_total_cores > 0) else 0

        # Memória
        try:
            memory_total_gb = sum(getattr(h.hardware, 'memorySize', 0) or 0 for h in hosts) / (1024**3)
        except Exception:
            memory_total_gb = 0

        try:
            memory_used_gb = sum(getattr(h.summary.quickStats, 'overallMemoryUsage', 0) or 0 for h in hosts) / 1024
        except Exception:
            memory_used_gb = 0

        memory_available_gb = memory_total_gb - memory_used_gb
        memory_usage_percent = (memory_used_gb / memory_total_gb * 100) if memory_total_gb > 0 else 0

        # VMs (ignora templates)
        vm_total = 0
        vm_on = 0
        vm_off = 0
        vcpu_total = 0
        vcpu_used = 0

        for h in hosts:
            try:
                vm_list = getattr(h, 'vm', []) or []
            except Exception:
                vm_list = []

            for vm in vm_list:
                try:
                    # ignora templates
                    if getattr(vm, 'config', None) and getattr(vm.config, 'template', False):
                        continue

                    vm_total += 1
                    num_cpu = getattr(vm.config.hardware, 'numCPU', 0) or 0
                    vcpu_total += num_cpu

                    power_state = getattr(vm.runtime, 'powerState', None)
                    if power_state and str(power_state) == "poweredOn":
                        vm_on += 1
                        vcpu_used += num_cpu
                    else:
                        vm_off += 1
                except Exception:
                    # ignora VM com dados incompletos
                    continue

        # Disco (datastores únicos)
        disk_total_gb = 0
        disk_free_gb = 0
        seen_ds = set()
        for h in hosts:
            try:
                ds_list = getattr(h, 'datastore', []) or []
            except Exception:
                ds_list = []
            for ds in ds_list:
                try:
                    ds_id = getattr(ds, '_moId', None)
                    if ds_id in seen_ds:
                        continue
                    seen_ds.add(ds_id)
                    disk_total_gb += (getattr(ds.summary, 'capacity', 0) or 0) / (1024**3)
                    disk_free_gb += (getattr(ds.summary, 'freeSpace', 0) or 0) / (1024**3)
                except Exception:
                    continue

        disk_used_gb = disk_total_gb - disk_free_gb
        disk_usage_percent = (disk_used_gb / disk_total_gb * 100) if disk_total_gb > 0 else 0

        # Monta a linha de métricas (26 colunas)
        metrics.append([
            "vcenter", level, name,
            cpu_usage_percent, cpu_total_cores, cpu_total_ghz,
            cpu_used_ghz, cpu_available_ghz, cpu_used_cores, 0,  # cpu_load_average = 0 (não coletado)
            memory_usage_percent, memory_total_gb, memory_used_gb, memory_available_gb,
            disk_usage_percent, disk_total_gb, disk_used_gb, disk_free_gb,
            0, 0,  # disk_iops_read, disk_iops_write = 0 (não coletado)
            vm_total, vm_on, vm_off,
            vcpu_total, vcpu_used,
            now
        ])

    # Percorre datacenters -> clusters -> hosts
    try:
        root = content.rootFolder
        if not getattr(root, 'childEntity', None):
            print("⚠️ vCenter: rootFolder sem childEntity")
        for datacenter in getattr(root, 'childEntity', []) or []:
            # Alguns ambientes podem ter objetos não-datacenters aqui; protegemos com try
            try:
                host_folder = getattr(datacenter, 'hostFolder', None)
                if not host_folder:
                    continue
                for cluster in getattr(host_folder, 'childEntity', []) or []:
                    if isinstance(cluster, vim.ClusterComputeResource):
                        print(f"ℹ️ Coletando cluster: {getattr(cluster, 'name', '<sem-nome>')}")
                        # Cluster (todos hosts do cluster)
                        hosts_in_cluster = getattr(cluster, 'host', []) or []
                        summarize_entity(cluster, "cluster", getattr(cluster, 'name', 'unknown'), hosts_in_cluster)

                        # Hosts individuais no cluster
                        for host in hosts_in_cluster:
                            try:
                                print(f"  - host: {getattr(host, 'name', '<sem-nome>')}")
                                summarize_entity(host, "host", getattr(host, 'name', 'unknown'), [host])
                            except Exception as e:
                                print(f"    ❌ Erro coletando host: {e}")
            except Exception as e:
                print(f"  ❌ Erro iterando datacenter {getattr(datacenter, 'name', '')}: {e}")
    except Exception as e:
        print(f"❌ Erro ao iterar sobre content.rootFolder: {e}")

    # Total vSphere (todos hosts coletados)
    try:
        all_hosts = []
        for datacenter in getattr(content.rootFolder, 'childEntity', []) or []:
            host_folder = getattr(datacenter, 'hostFolder', None)
            if not host_folder:
                continue
            for cluster in getattr(host_folder, 'childEntity', []) or []:
                if isinstance(cluster, vim.ClusterComputeResource):
                    all_hosts.extend(getattr(cluster, 'host', []) or [])
        if all_hosts:
            print("ℹ️ Coletando resumo Total-vSphere")
            summarize_entity("vcenter", "vcenter", "Total-vSphere", all_hosts)
    except Exception as e:
        print(f"❌ Erro ao coletar Total-vSphere: {e}")

    print(f"▶ Coleta finalizada: {len(metrics)} métricas coletadas.")
    return metrics


def save_vcenter_metrics(metrics):
    # espera-se que cada linha tenha 26 colunas (conforme append em summarize_entity)
    EXPECTED_COLS = 26

    # valida e filtra
    metrics = validate_metrics_list(metrics, EXPECTED_COLS)
    if not metrics:
        print("⚠️ Nenhuma métrica válida para inserir. Abortando inserção.")
        return

    conn = None
    cur = None
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST, dbname=POSTGRES_DB, user=POSTGRES_USER,
            password=POSTGRES_PASSWORD, port=POSTGRES_PORT
        )
        cur = conn.cursor()
        print("▶ Criando tabela vcenter_metrics (se não existir)...")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS vcenter_metrics (
                id SERIAL PRIMARY KEY,
                source TEXT, level TEXT, name TEXT,
                cpu_usage_percent FLOAT, cpu_total_cores INT, cpu_total_ghz FLOAT,
                cpu_used_ghz FLOAT, cpu_available_ghz FLOAT, cpu_used_cores FLOAT, cpu_load_average FLOAT,
                memory_usage_percent FLOAT, memory_total_gb FLOAT, memory_used_gb FLOAT, memory_available_gb FLOAT,
                disk_usage_percent FLOAT, disk_total_gb FLOAT, disk_used_gb FLOAT, disk_available_gb FLOAT,
                disk_iops_read FLOAT, disk_iops_write FLOAT,
                vm_total INT, vm_on INT, vm_off INT,
                vcpu_total INT, vcpu_used INT, data_coleta TIMESTAMP
            );
        """)

        insert_sql = """
            INSERT INTO vcenter_metrics (
                source, level, name, cpu_usage_percent, cpu_total_cores, cpu_total_ghz,
                cpu_used_ghz, cpu_available_ghz, cpu_used_cores, cpu_load_average,
                memory_usage_percent, memory_total_gb, memory_used_gb, memory_available_gb,
                disk_usage_percent, disk_total_gb, disk_used_gb, disk_available_gb,
                disk_iops_read, disk_iops_write,
                vm_total, vm_on, vm_off,
                vcpu_total, vcpu_used, data_coleta
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s
            );
        """

        print(f"▶ Inserindo {len(metrics)} métricas no banco...")
        cur.executemany(insert_sql, metrics)
        conn.commit()
        print("✅ Inserção concluída com sucesso.")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ ERRO AO SALVAR MÉTRICAS: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    print("▶ Iniciando coleta vCenter...")
    data = collect_vcenter_metrics()
    print(f"▶ Métricas coletadas: {len(data)}")

    if not data:
        print("⚠️ Nenhuma métrica coletada. Verifique conexão/credenciais/vCenter.")
    else:
        # Salva apenas se tiver métricas válidas
        save_vcenter_metrics(data)
