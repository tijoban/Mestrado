import requests
import psycopg2
from datetime import datetime, timezone
import warnings
from env import (
    PRISM_URL, NUTANIX_USER, NUTANIX_PASSWORD,
    POSTGRES_HOST, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB, POSTGRES_PORT
)

# Suprimir warnings de SSL
requests.packages.urllib3.disable_warnings()
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

def bytes_to_gb(b):
    return round(b / (1024 ** 3), 2) if b and b > 0 else 0

def hz_to_ghz(hz):
    return round(hz / 1_000_000_000, 2) if hz and hz > 0 else 0

def get_data(session, url):
    try:
        response = session.get(url, timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"[ERRO] {url}: {response.status_code} {response.text}")
            return {}
    except Exception as e:
        print(f"[ERRO] {e}")
        return {}

def calculate_uptime(boot_time_usecs):
    """Calcula uptime a partir do tempo de boot em microsegundos"""
    if boot_time_usecs and boot_time_usecs > 0:
        boot_time_secs = boot_time_usecs / 1_000_000
        current_time_secs = datetime.now().timestamp()
        uptime_sec = current_time_secs - boot_time_secs
        return int(uptime_sec)
    return 0

def analyze_host_disks(host_data):
    """Analisa informações de disco do host"""
    disks_info = {
        'total_disks': 0,
        'boot_disks': 0,
        'data_disks': 0,
        'total_disk_capacity_bytes': 0,
        'used_disk_capacity_bytes': 0,
        'disk_models': set(),
        'ssd_disks': 0,
        'hdd_disks': 0
    }
    
    # Tentar diferentes formatos de chave para discos
    disk_configs = host_data.get('disk_hardware_configs') or host_data.get('diskHardwareConfigs', {})
    
    if disk_configs:
        disks_info['total_disks'] = len(disk_configs)
        
        for disk_id, disk in disk_configs.items():
            # Verificar se é disco de boot
            is_boot_disk = disk.get('boot_disk') or disk.get('bootDisk', False)
            if is_boot_disk:
                disks_info['boot_disks'] += 1
            else:
                disks_info['data_disks'] += 1
            
            # Identificar tipo de disco pelo modelo
            model = disk.get('model', '').upper()
            if any(ssd_indicator in model for ssd_indicator in ['SSD', 'NVME', 'M.2', 'SAMSUNG MZQL']):
                disks_info['ssd_disks'] += 1
            else:
                disks_info['hdd_disks'] += 1
            
            # Coletar modelos
            if model and 'NOT AVAILABLE' not in model:
                disks_info['disk_models'].add(model)
    
    # Coletar estatísticas de uso de disco do host
    usage_stats = host_data.get('usage_stats') or host_data.get('usageStats', {})
    if usage_stats:
        if isinstance(usage_stats, dict):
            disks_info['total_disk_capacity_bytes'] = int(usage_stats.get('storage.capacity_bytes', 0))
            disks_info['used_disk_capacity_bytes'] = int(usage_stats.get('storage.usage_bytes', 0))
    
    disks_info['disk_models'] = list(disks_info['disk_models'])
    
    return disks_info

def collect_nutanix_metrics():
    session = requests.Session()
    session.auth = (NUTANIX_USER, NUTANIX_PASSWORD)
    session.verify = False

    metrics = []
    now = datetime.now(timezone.utc)

    # Coletar dados
    cluster_data = get_data(session, f"{PRISM_URL}/cluster")
    hosts_data = get_data(session, f"{PRISM_URL}/hosts")
    vms_data = get_data(session, f"{PRISM_URL}/vms")
    disks_data = get_data(session, f"{PRISM_URL}/disks")

    clusters = [cluster_data] if cluster_data else []  # Cluster único no Prism Element
    hosts_entities = hosts_data.get("entities", [])
    vms_entities = vms_data.get("entities", [])
    disks_entities = disks_data.get("entities", [])

    # ----------- MÉTRICAS DO CLUSTER -----------
    for cluster in clusters:
        cluster_name = cluster.get("name", "unknown")
        cluster_stats = cluster.get("stats", {})
        cluster_usage = cluster.get("usage_stats", {})

        # Calcular uptime do cluster
        cluster_uptime_sec = calculate_uptime(cluster.get("boot_time_in_usecs"))

        # CPU - cálculo mais preciso
        total_cpu_hz = sum(h.get("cpu_capacity_in_hz", 0) for h in hosts_entities)
        cpu_total_ghz = hz_to_ghz(total_cpu_hz)
        cpu_usage_percent = float(cluster_stats.get('hypervisor_cpu_usage_ppm', 0)) / 10000
        cpu_used_ghz = round((cpu_total_ghz * cpu_usage_percent) / 100, 2) if cpu_total_ghz else 0
        cpu_total_cores = sum(h.get("num_cpu_cores", 0) for h in hosts_entities)
        cpu_used_cores = round((cpu_usage_percent / 100) * cpu_total_cores, 2)

        # Memória
        total_memory_bytes = sum(h.get("memory_capacity_in_bytes", 0) for h in hosts_entities)
        memory_total_gb = bytes_to_gb(total_memory_bytes)
        memory_usage_percent = float(cluster_stats.get('hypervisor_memory_usage_ppm', 0)) / 10000
        memory_used_gb = round((memory_total_gb * memory_usage_percent) / 100, 2)
        memory_available_gb = memory_total_gb - memory_used_gb

        # Storage do cluster
        storage_total_bytes = int(cluster_usage.get("storage.capacity_bytes", 0))
        storage_used_bytes = int(cluster_usage.get("storage.usage_bytes", 0))
        storage_available_bytes = storage_total_bytes - storage_used_bytes

        storage_total_gb = bytes_to_gb(storage_total_bytes)
        storage_used_gb = bytes_to_gb(storage_used_bytes)
        storage_available_gb = bytes_to_gb(storage_available_bytes)
        disk_usage_percent = round((storage_used_gb / storage_total_gb * 100), 2) if storage_total_gb > 0 else 0

        # Estatísticas de IO do cluster
        disk_iops_read = float(cluster_stats.get('num_read_iops', 0))
        disk_iops_write = float(cluster_stats.get('num_write_iops', 0))

        # VMs
        vm_total = len(vms_entities)
        vm_on = sum(1 for vm in vms_entities if str(vm.get("power_state", "")).lower() in ["on", "kon"])
        vm_off = vm_total - vm_on

        # Hit rate
        hit_rate_percent = float(cluster_stats.get('content_cache_hit_ppm', 0)) / 10000

        # Informações de disco do cluster (somatório dos hosts)
        total_cluster_disks = 0
        total_cluster_boot_disks = 0
        total_cluster_data_disks = 0
        
        for host in hosts_entities:
            disk_info = analyze_host_disks(host)
            total_cluster_disks += disk_info['total_disks']
            total_cluster_boot_disks += disk_info['boot_disks']
            total_cluster_data_disks += disk_info['data_disks']

        metrics.append([
            "nutanix", "cluster", cluster_name,
            cpu_usage_percent, cpu_total_cores, cpu_total_ghz,
            cpu_used_ghz, cpu_total_ghz - cpu_used_ghz, cpu_used_cores, 0,  # cpu_load_average = 0
            memory_usage_percent, memory_total_gb, memory_used_gb, memory_available_gb,
            disk_usage_percent, storage_total_gb, storage_used_gb, storage_available_gb,
            disk_iops_read, disk_iops_write,
            vm_total, vm_on, vm_off,
            cluster_uptime_sec, hit_rate_percent, len(hosts_entities),
            total_cluster_disks, total_cluster_boot_disks, total_cluster_data_disks,
            now
        ])

    # ----------- MÉTRICAS DOS HOSTS -----------
    for host in hosts_entities:
        host_name = host.get("name", "unknown")
        stats = host.get("stats", {})
        
        # Calcular uptime do host
        host_uptime_sec = calculate_uptime(host.get("boot_time_in_usecs"))
        
        # Analisar informações de disco do host
        disk_info = analyze_host_disks(host)
        
        # CPU
        cpu_capacity_hz = host.get("cpu_capacity_in_hz", 0)
        cpu_total_ghz = hz_to_ghz(cpu_capacity_hz)
        cpu_usage_percent = float(stats.get("hypervisor_cpu_usage_ppm", 0)) / 10000
        cpu_used_ghz = round((cpu_total_ghz * cpu_usage_percent) / 100, 2) if cpu_total_ghz else 0
        cpu_total_cores = host.get("num_cpu_cores", 0)
        cpu_used_cores = round((cpu_usage_percent / 100) * cpu_total_cores, 2)

        # Memória
        memory_capacity_bytes = host.get("memory_capacity_in_bytes", 0)
        memory_total_gb = bytes_to_gb(memory_capacity_bytes)
        memory_usage_percent = float(stats.get("hypervisor_memory_usage_ppm", 0)) / 10000
        memory_used_gb = round((memory_total_gb * memory_usage_percent) / 100, 2)
        memory_available_gb = memory_total_gb - memory_used_gb

        # Storage do host
        host_disk_total_gb = bytes_to_gb(disk_info['total_disk_capacity_bytes'])
        host_disk_used_gb = bytes_to_gb(disk_info['used_disk_capacity_bytes'])
        host_disk_available_gb = host_disk_total_gb - host_disk_used_gb
        host_disk_usage_percent = round((host_disk_used_gb / host_disk_total_gb * 100), 2) if host_disk_total_gb > 0 else 0

        # Estatísticas de IO do host
        host_disk_iops_read = float(stats.get('num_read_iops', 0))
        host_disk_iops_write = float(stats.get('num_write_iops', 0))

        # Número de VMs no host
        host_vm_total = host.get("num_vms", 0)
        # Nota: Para VMs on/off por host, precisaríamos de uma lógica mais complexa

        # Hit rate do host
        host_hit_rate_percent = float(stats.get('content_cache_hit_ppm', 0)) / 10000

        metrics.append([
            "nutanix", "host", host_name,
            cpu_usage_percent, cpu_total_cores, cpu_total_ghz,
            cpu_used_ghz, cpu_total_ghz - cpu_used_ghz, cpu_used_cores, 0,
            memory_usage_percent, memory_total_gb, memory_used_gb, memory_available_gb,
            host_disk_usage_percent, host_disk_total_gb, host_disk_used_gb, host_disk_available_gb,
            host_disk_iops_read, host_disk_iops_write,
            host_vm_total, 0, 0,  # vm_on e vm_off não disponíveis por host
            host_uptime_sec, host_hit_rate_percent, 1,  # num_hosts = 1 para host individual
            disk_info['total_disks'], disk_info['boot_disks'], disk_info['data_disks'],
            now
        ])

    return metrics

def save_nutanix_metrics(metrics):
    conn = psycopg2.connect(
        host=POSTGRES_HOST, dbname=POSTGRES_DB, user=POSTGRES_USER,
        password=POSTGRES_PASSWORD, port=POSTGRES_PORT
    )
    cur = conn.cursor()

    # Verificar se a tabela existe e adicionar colunas faltantes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nutanix_metrics (
            id SERIAL PRIMARY KEY,
            source TEXT, level TEXT, name TEXT,
            cpu_usage_percent FLOAT, cpu_total_cores INT, cpu_total_ghz FLOAT,
            cpu_used_ghz FLOAT, cpu_available_ghz FLOAT, cpu_used_cores FLOAT, cpu_load_average FLOAT,
            memory_usage_percent FLOAT, memory_total_gb FLOAT, memory_used_gb FLOAT, memory_available_gb FLOAT,
            disk_usage_percent FLOAT, disk_total_gb FLOAT, disk_used_gb FLOAT, disk_available_gb FLOAT,
            disk_iops_read FLOAT, disk_iops_write FLOAT,
            vm_total INT, vm_on INT, vm_off INT,
            uptime_sec BIGINT, hit_rate_percent FLOAT, num_hosts INT,
            total_disks INT, boot_disks INT, data_disks INT,
            data_coleta TIMESTAMP
        );
    """)

    # Adicionar colunas faltantes se não existirem
    cur.execute("""
        DO $$ 
        BEGIN 
            -- Verificar e adicionar colunas de disco
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='total_disks') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN total_disks INT;
            END IF;
            
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='boot_disks') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN boot_disks INT;
            END IF;
            
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='data_disks') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN data_disks INT;
            END IF;
            
            -- Verificar colunas existentes (para compatibilidade)
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='uptime_sec') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN uptime_sec BIGINT;
            END IF;
            
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='hit_rate_percent') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN hit_rate_percent FLOAT;
            END IF;
            
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='nutanix_metrics' AND column_name='num_hosts') THEN
                ALTER TABLE nutanix_metrics ADD COLUMN num_hosts INT;
            END IF;
        END $$;
    """)

    insert_sql = """
        INSERT INTO nutanix_metrics (
            source, level, name, cpu_usage_percent, cpu_total_cores, cpu_total_ghz,
            cpu_used_ghz, cpu_available_ghz, cpu_used_cores, cpu_load_average,
            memory_usage_percent, memory_total_gb, memory_used_gb, memory_available_gb,
            disk_usage_percent, disk_total_gb, disk_used_gb, disk_available_gb,
            disk_iops_read, disk_iops_write, vm_total, vm_on, vm_off,
            uptime_sec, hit_rate_percent, num_hosts, total_disks, boot_disks, data_disks, data_coleta
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
    """
    
    cur.executemany(insert_sql, metrics)
    conn.commit()
    cur.close()
    conn.close()

def print_metrics_summary(metrics):
    """Exibe um resumo das métricas coletadas"""
    print(f"\n📊 RESUMO DAS MÉTRICAS COLETADAS")
    print("="*60)
    
    cluster_metrics = [m for m in metrics if m[2] == 'cluster']
    host_metrics = [m for m in metrics if m[2] == 'host']
    
    for metric in cluster_metrics:
        print(f"\n🏢 CLUSTER: {metric[3]}")
        print(f"   🔸 Hosts: {metric[26]}")
        print(f"   🔸 Discos: Total {metric[27]}, Boot {metric[28]}, Dados {metric[29]}")
        print(f"   🔸 Uptime: {metric[23] // 3600} horas")
        print(f"   🔸 VMs: {metric[21]} total, {metric[22]} ligadas")
        print(f"   🔸 CPU: {metric[4]:.1f}% uso")
        print(f"   🔸 Memória: {metric[11]:.1f}% uso")
        print(f"   🔸 Storage: {metric[15]:.1f}% uso")
    
    for metric in host_metrics:
        print(f"\n🖥️  HOST: {metric[3]}")
        print(f"   🔸 Discos: Total {metric[27]}, Boot {metric[28]}, Dados {metric[29]}")
        print(f"   🔸 Uptime: {metric[23] // 3600} horas")
        print(f"   🔸 VMs: {metric[21]} no host")
        print(f"   🔸 CPU: {metric[4]:.1f}% uso")
        print(f"   🔸 Memória: {metric[11]:.1f}% uso")

if __name__ == "__main__":
    print("🚀 Coletando métricas do Nutanix...")
    data = collect_nutanix_metrics()
    
    if data:
        save_nutanix_metrics(data)
        print_metrics_summary(data)
        print(f"\n✅ Coletadas {len(data)} métricas do Nutanix")
        print(f"💾 Métricas salvas no banco de dados")
    else:
        print("❌ Nenhuma métrica foi coletada")