from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
import psycopg2
import env  # Arquivo com suas credenciais (VCENTER_..., POST_...)

# ==============================================================================
# 1. BANCO DE DADOS (POSTQL)
# ==============================================================================

def connect_db():
    # Conecta forçando o schema public
    return psycopg2.connect(
        host=env.POST_HOST,
        port=env.POST_PORT,
        dbname=env.POST_DB,
        user=env.POST_USER,
        password=env.POST_PASSWORD,
        options="-c search_path=public"
    )

def create_tables(conn):
    """
    Cria as tabelas caso não existam.
    Agora não apagamos mais os dados antigos para construir um histórico de métricas.
    """
    with conn.cursor() as cur:
        # Tabela 1: Detalhes (Lista de VMs)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.vm_metrics_detalhado (
                id SERIAL PRIMARY KEY,
                folder_nome VARCHAR(100),
                vm_nome VARCHAR(255),
                status VARCHAR(20),
                vcpu INT,
                ram_gb NUMERIC(10,2),
                disk_used_gb NUMERIC(10,2),
                disk_provisioned_gb NUMERIC(10,2),
                datastores TEXT,
                data_coleta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Tabela 2: Resumo (Totais por Pasta/Cliente)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.folder_metrics_resumo (
                id SERIAL PRIMARY KEY,
                folder_nome VARCHAR(100),
                qtd_vms INT,
                vms_on INT,
                vms_off INT,
                total_vcpu INT,
                total_ram_gb NUMERIC(10,2),
                total_disk_used_gb NUMERIC(10,2),
                total_disk_prov_gb NUMERIC(10,2),
                data_coleta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

def insert_vm_metric(conn, folder_name, vm_data):
    """Insere dados da VM individual."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.vm_metrics_detalhado (
                folder_nome, vm_nome, status, vcpu, ram_gb,
                disk_used_gb, disk_provisioned_gb, datastores
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            folder_name,
            vm_data['name'],
            vm_data['status'],
            vm_data['vcpu'],
            vm_data['ram_gb'],
            vm_data['disk_used_gb'],
            vm_data['disk_prov_gb'],
            vm_data['datastores']
        ))
        conn.commit()

def insert_folder_summary(conn, folder_name, summary_data):
    """Insere o resumo consolidado da pasta (Tudo em GB agora)."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.folder_metrics_resumo (
                folder_nome, qtd_vms, vms_on, vms_off, total_vcpu, total_ram_gb,
                total_disk_used_gb, total_disk_prov_gb
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            folder_name,
            summary_data['qtd_vms'],
            summary_data['vms_on'],
            summary_data['vms_off'],
            summary_data['total_vcpu'],
            summary_data['total_ram_gb'],
            summary_data['total_disk_used_gb'], # Valor em GB
            summary_data['total_disk_prov_gb']  # Valor em GB
        ))
        conn.commit()

# ==============================================================================
# 2. LÓGICA VMWARE
# ==============================================================================

def get_datastore_names(vm):
    try:
        datastores = [ds.name for ds in vm.datastore]
        return ", ".join(datastores)
    except:
        return "N/A"

def get_vms_recursive(folder):
    """Busca VMs recursivamente, ignorando Templates."""
    vms = []
    if hasattr(folder, 'childEntity'):
        for child in folder.childEntity:
            if isinstance(child, vim.VirtualMachine):
                if not child.config.template:
                    vms.append(child)
            elif isinstance(child, vim.Folder):
                vms.extend(get_vms_recursive(child))
    return vms

def process_folder_and_save(conn, folder_name, vms):
    if not vms:
        return

    print(f"> Processando Pasta: [{folder_name}] - {len(vms)} VMs.")

    # Acumuladores
    sum_vcpu = 0
    sum_ram_gb = 0.0
    sum_disk_used_gb = 0.0
    sum_disk_prov_gb = 0.0

    count_on = 0
    count_off = 0

    # 1. Processa cada VM
    for vm in vms:
        try:
            summary = vm.summary
            config = summary.config
            storage = summary.storage

            # Status
            is_on = (vm.runtime.powerState == "poweredOn")
            status_str = "LIGADA" if is_on else "DESLIGADA"

            if is_on: count_on += 1
            else: count_off += 1

            # Recursos (Raw Numbers)
            vcpu = config.numCpu
            ram_mb = float(config.memorySizeMB)
            ram_gb = ram_mb / 1024.0 # Converter MB para GB

            committed = float(storage.committed or 0)
            uncommitted = float(storage.uncommitted or 0)

            # Converter Bytes para GB
            disk_used_gb = committed / (1024**3)
            disk_prov_gb = (committed + uncommitted) / (1024**3)

            # Soma Totais (Mantendo em GB)
            sum_vcpu += vcpu
            sum_ram_gb += ram_gb
            sum_disk_used_gb += disk_used_gb
            sum_disk_prov_gb += disk_prov_gb

            # Insere VM (Detalhe)
            vm_data = {
                "name": config.name,
                "status": status_str,
                "vcpu": vcpu,
                "ram_gb": round(ram_gb, 2),
                "disk_used_gb": round(disk_used_gb, 2),
                "disk_prov_gb": round(disk_prov_gb, 2),
                "datastores": get_datastore_names(vm)
            }
            insert_vm_metric(conn, folder_name, vm_data)

        except Exception as e:
            print(f"  [ERRO] VM {vm.name if 'vm' in locals() else '?'}: {e}")

    # 2. Insere Resumo (Totais)
    summary_data = {
        "qtd_vms": len(vms),
        "vms_on": count_on,
        "vms_off": count_off,
        "total_vcpu": sum_vcpu,
        "total_ram_gb": round(sum_ram_gb, 2),
        "total_disk_used_gb": round(sum_disk_used_gb, 2), # GB Puro
        "total_disk_prov_gb": round(sum_disk_prov_gb, 2)  # GB Puro
    }

    insert_folder_summary(conn, folder_name, summary_data)
    print(f"  -> Resumo salvo: {count_on} ON | {count_off} OFF | {summary_data['total_disk_prov_gb']} GB Total")

# ==============================================================================
# 3. MAIN
# ==============================================================================

def main():
    print("--- INICIANDO COLETA (PADRÃO GB) ---")

    # 1. vCenter
    print("Conectando ao vCenter...")
    si = SmartConnect(
        host=env.VCENTER_HOST,
        user=env.VCENTER_USER,
        pwd=env.VCENTER_PASSWORD,
        port=env.VCENTER_PORT,
        sslContext=ssl._create_unverified_context()
    )
    content = si.RetrieveContent()

    # 2. Banco
    print("Conectando ao POSTQL...")
    db = connect_db()
    create_tables(db)

    print("\n>>> Iniciando Varredura...")

    # 3. Varredura
    for dc in content.rootFolder.childEntity:
        if hasattr(dc, 'vmFolder'):
            for child in dc.vmFolder.childEntity:

                # Pastas de Clientes
                if isinstance(child, vim.Folder):
                    folder_name = child.name
                    vms_in_folder = get_vms_recursive(child)
                    process_folder_and_save(db, folder_name, vms_in_folder)

                # VMs soltas
                elif isinstance(child, vim.VirtualMachine):
                    if not child.config.template:
                        process_folder_and_save(db, "Raiz (Sem Pasta)", [child])

    db.close()
    Disconnect(si)
    print("\n--- SUCESSO! DADOS EM GB PRONTOS ---")

if __name__ == "__main__":
    main()
