-- Tabelas gravadas pela coleta (banco canônico).
-- 1) datacenter: coleta/datacenter_collector.py

-- ============================================================
-- schema.sql
-- Recriação completa da tabela `datacenter` com colunas de
-- uso físico (bruto + resiliente) e provisionamento (overcommit).
-- ============================================================
-- ATENÇÃO: apaga TODO o histórico anterior.
-- ============================================================

DROP TABLE IF EXISTS datacenter CASCADE;

CREATE TABLE datacenter (
    id                          BIGSERIAL PRIMARY KEY,
    data_coleta                 TIMESTAMPTZ NOT NULL,

    -- Dimensões
    plataforma                  VARCHAR(32)  NOT NULL,   -- vmware | nutanix | ALL
    cluster                     VARCHAR(128) NOT NULL,
    tipo_storage                VARCHAR(16)  NOT NULL,   -- FAST | SLOW | ALL
    scope                       VARCHAR(16)  NOT NULL,   -- TOTAL | PLATFORM | CLUSTER | STORAGE
    name                        VARCHAR(128) NOT NULL,

    -- ==========================================================
    -- CPU
    -- ==========================================================
    cpu_total_cores             INTEGER       NOT NULL DEFAULT 0,
    cpu_total_ghz               NUMERIC(14,2) NOT NULL DEFAULT 0,
    cpu_used_ghz                NUMERIC(14,2) NOT NULL DEFAULT 0,
    cpu_free_ghz                NUMERIC(14,2) NOT NULL DEFAULT 0,
    cpu_usage_percent           NUMERIC(8,2)  NOT NULL DEFAULT 0,
    -- Provisionamento (overcommit) — pode > 100%
    cpu_provisioned_ghz         NUMERIC(14,2) NOT NULL DEFAULT 0,
    cpu_provisioned_percent     NUMERIC(8,2)  NOT NULL DEFAULT 0,

    -- ==========================================================
    -- MEMÓRIA
    -- ==========================================================
    memory_total_gb             NUMERIC(14,2) NOT NULL DEFAULT 0,
    memory_used_gb              NUMERIC(14,2) NOT NULL DEFAULT 0,
    memory_free_gb              NUMERIC(14,2) NOT NULL DEFAULT 0,
    memory_usage_percent        NUMERIC(8,2)  NOT NULL DEFAULT 0,
    -- Provisionamento (overcommit) — pode > 100%
    memory_provisioned_gb       NUMERIC(14,2) NOT NULL DEFAULT 0,
    memory_provisioned_percent  NUMERIC(8,2)  NOT NULL DEFAULT 0,

    -- ==========================================================
    -- STORAGE FAST (vSAN / Nutanix / Pure NVMe / NETAPP-SSD)
    -- ==========================================================
    -- Bruto
    storage_fast_total_gb               NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_used_gb                NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_free_gb                NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_usage_percent          NUMERIC(8,2)  NOT NULL DEFAULT 0,
    -- Provisionamento (thin-prov pode exceder 100%)
    storage_fast_provisioned_gb         NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_provisioned_percent    NUMERIC(8,2)  NOT NULL DEFAULT 0,
    -- Resiliente (Nutanix: após reserva de rebuild; VMware: igual ao bruto)
    storage_fast_resilient_gb               NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_resilient_used_gb          NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_resilient_free_gb          NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_fast_resilient_usage_percent    NUMERIC(8,2)  NOT NULL DEFAULT 0,

    -- ==========================================================
    -- STORAGE SLOW (NETAPP-NL)
    -- ==========================================================
    storage_slow_total_gb               NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_used_gb                NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_free_gb                NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_usage_percent          NUMERIC(8,2)  NOT NULL DEFAULT 0,
    storage_slow_provisioned_gb         NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_provisioned_percent    NUMERIC(8,2)  NOT NULL DEFAULT 0,
    storage_slow_resilient_gb               NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_resilient_used_gb          NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_resilient_free_gb          NUMERIC(16,2) NOT NULL DEFAULT 0,
    storage_slow_resilient_usage_percent    NUMERIC(8,2)  NOT NULL DEFAULT 0,

    -- ==========================================================
    -- VMs / vCPU
    -- ==========================================================
    vm_total                    INTEGER NOT NULL DEFAULT 0,
    vm_on                       INTEGER NOT NULL DEFAULT 0,
    vm_off                      INTEGER NOT NULL DEFAULT 0,
    vcpu_total                  INTEGER NOT NULL DEFAULT 0,
    vcpu_on                     INTEGER NOT NULL DEFAULT 0,
    vcpu_off                    INTEGER NOT NULL DEFAULT 0,
    vcpu_provisioned            INTEGER NOT NULL DEFAULT 0
);

COMMENT ON TABLE datacenter IS
'Snapshots históricos consolidados de capacidade de Data Center (VMware + Nutanix). Sem update/delete: cada coleta gera novas linhas.';


-- ============================================================
-- ÍNDICES
-- ============================================================
CREATE INDEX idx_dc_data_coleta        ON datacenter (data_coleta DESC);
CREATE INDEX idx_dc_scope              ON datacenter (scope);
CREATE INDEX idx_dc_plataforma         ON datacenter (plataforma);
CREATE INDEX idx_dc_cluster            ON datacenter (cluster);
CREATE INDEX idx_dc_tipo_storage       ON datacenter (tipo_storage);
CREATE INDEX idx_dc_scope_data         ON datacenter (scope, data_coleta DESC);
CREATE INDEX idx_dc_plat_data          ON datacenter (plataforma, data_coleta DESC);
CREATE INDEX idx_dc_cluster_data       ON datacenter (cluster, data_coleta DESC);


-- ============================================================
-- VIEWS
-- ============================================================

-- Última foto do TOTAL geral
CREATE VIEW vw_datacenter_total AS
SELECT
    data_coleta,
    cpu_total_cores, cpu_total_ghz, cpu_used_ghz, cpu_free_ghz, cpu_usage_percent,
    cpu_provisioned_ghz, cpu_provisioned_percent,
    memory_total_gb, memory_used_gb, memory_free_gb, memory_usage_percent,
    memory_provisioned_gb, memory_provisioned_percent,
    storage_fast_total_gb, storage_fast_used_gb,
    storage_fast_free_gb, storage_fast_usage_percent,
    storage_fast_provisioned_gb, storage_fast_provisioned_percent,
    storage_fast_resilient_gb, storage_fast_resilient_used_gb,
    storage_fast_resilient_free_gb, storage_fast_resilient_usage_percent,
    storage_slow_total_gb, storage_slow_used_gb,
    storage_slow_free_gb, storage_slow_usage_percent,
    storage_slow_provisioned_gb, storage_slow_provisioned_percent,
    storage_slow_resilient_gb, storage_slow_resilient_used_gb,
    storage_slow_resilient_free_gb, storage_slow_resilient_usage_percent,
    (storage_fast_total_gb + storage_slow_total_gb)             AS storage_total_gb,
    (storage_fast_used_gb  + storage_slow_used_gb)              AS storage_used_gb,
    (storage_fast_free_gb  + storage_slow_free_gb)              AS storage_free_gb,
    (storage_fast_provisioned_gb + storage_slow_provisioned_gb) AS storage_provisioned_gb,
    CASE WHEN (storage_fast_total_gb + storage_slow_total_gb) > 0
         THEN ROUND(((storage_fast_used_gb + storage_slow_used_gb) * 100.0)
                  / (storage_fast_total_gb + storage_slow_total_gb), 2)
         ELSE 0 END                                             AS storage_usage_percent,
    CASE WHEN (storage_fast_total_gb + storage_slow_total_gb) > 0
         THEN ROUND(((storage_fast_provisioned_gb + storage_slow_provisioned_gb) * 100.0)
                  / (storage_fast_total_gb + storage_slow_total_gb), 2)
         ELSE 0 END                                             AS storage_provisioned_percent,
    vm_total, vm_on, vm_off,
    vcpu_total, vcpu_on, vcpu_off, vcpu_provisioned
FROM datacenter
WHERE scope = 'TOTAL'
ORDER BY data_coleta DESC;

-- Última foto por plataforma
CREATE VIEW vw_datacenter_platform AS
SELECT
    data_coleta, plataforma, name,
    cpu_total_cores, cpu_total_ghz, cpu_used_ghz, cpu_free_ghz, cpu_usage_percent,
    cpu_provisioned_ghz, cpu_provisioned_percent,
    memory_total_gb, memory_used_gb, memory_free_gb, memory_usage_percent,
    memory_provisioned_gb, memory_provisioned_percent,
    storage_fast_total_gb, storage_fast_used_gb,
    storage_fast_free_gb, storage_fast_usage_percent,
    storage_fast_provisioned_gb, storage_fast_provisioned_percent,
    storage_fast_resilient_gb, storage_fast_resilient_used_gb,
    storage_fast_resilient_free_gb, storage_fast_resilient_usage_percent,
    storage_slow_total_gb, storage_slow_used_gb,
    storage_slow_free_gb, storage_slow_usage_percent,
    storage_slow_provisioned_gb, storage_slow_provisioned_percent,
    storage_slow_resilient_gb, storage_slow_resilient_used_gb,
    storage_slow_resilient_free_gb, storage_slow_resilient_usage_percent,
    vm_total, vm_on, vm_off,
    vcpu_total, vcpu_on, vcpu_off, vcpu_provisioned
FROM datacenter
WHERE scope = 'PLATFORM'
ORDER BY data_coleta DESC, plataforma;

-- Última foto por tipo de storage (FAST/SLOW)
CREATE VIEW vw_datacenter_storage AS
SELECT
    data_coleta, tipo_storage, name,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_total_gb            ELSE storage_slow_total_gb            END AS total_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_used_gb             ELSE storage_slow_used_gb             END AS used_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_free_gb             ELSE storage_slow_free_gb             END AS free_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_usage_percent       ELSE storage_slow_usage_percent       END AS usage_percent,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_provisioned_gb      ELSE storage_slow_provisioned_gb      END AS provisioned_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_provisioned_percent ELSE storage_slow_provisioned_percent END AS provisioned_percent,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_resilient_gb        ELSE storage_slow_resilient_gb        END AS resilient_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_resilient_used_gb   ELSE storage_slow_resilient_used_gb   END AS resilient_used_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_resilient_free_gb   ELSE storage_slow_resilient_free_gb   END AS resilient_free_gb,
    CASE WHEN tipo_storage='FAST' THEN storage_fast_resilient_usage_percent ELSE storage_slow_resilient_usage_percent END AS resilient_usage_percent
FROM datacenter
WHERE scope = 'STORAGE'
ORDER BY data_coleta DESC, tipo_storage;

-- Última foto por cluster
CREATE VIEW vw_datacenter_cluster AS
SELECT DISTINCT ON (cluster)
    data_coleta, plataforma, cluster, name,
    cpu_total_cores, cpu_total_ghz, cpu_used_ghz, cpu_free_ghz, cpu_usage_percent,
    cpu_provisioned_ghz, cpu_provisioned_percent,
    memory_total_gb, memory_used_gb, memory_free_gb, memory_usage_percent,
    memory_provisioned_gb, memory_provisioned_percent,
    storage_fast_total_gb, storage_fast_used_gb,
    storage_fast_free_gb, storage_fast_usage_percent,
    storage_fast_provisioned_gb,
    storage_slow_total_gb, storage_slow_used_gb,
    storage_slow_free_gb, storage_slow_usage_percent,
    storage_slow_provisioned_gb,
    vm_total, vm_on, vm_off,
    vcpu_total, vcpu_on, vcpu_off, vcpu_provisioned
FROM datacenter
WHERE scope = 'CLUSTER'
ORDER BY cluster, data_coleta DESC;

-- Série temporal — TOTAL
CREATE VIEW vw_datacenter_total_history AS
SELECT
    data_coleta AS "time",
    cpu_usage_percent, cpu_provisioned_percent,
    memory_usage_percent, memory_provisioned_percent,
    storage_fast_usage_percent, storage_fast_provisioned_percent,
    storage_slow_usage_percent, storage_slow_provisioned_percent,
    storage_fast_resilient_usage_percent, storage_slow_resilient_usage_percent,
    cpu_used_ghz, cpu_total_ghz, cpu_provisioned_ghz,
    memory_used_gb, memory_total_gb, memory_provisioned_gb,
    storage_fast_used_gb, storage_fast_total_gb, storage_fast_provisioned_gb,
    storage_fast_resilient_used_gb, storage_fast_resilient_gb,
    storage_slow_used_gb, storage_slow_total_gb, storage_slow_provisioned_gb,
    storage_slow_resilient_used_gb, storage_slow_resilient_gb,
    vm_total, vm_on, vm_off, vcpu_total, vcpu_provisioned
FROM datacenter
WHERE scope = 'TOTAL'
ORDER BY data_coleta;

-- Série temporal — por plataforma
CREATE VIEW vw_datacenter_platform_history AS
SELECT
    data_coleta AS "time", plataforma,
    cpu_usage_percent, cpu_provisioned_percent,
    memory_usage_percent, memory_provisioned_percent,
    storage_fast_usage_percent, storage_slow_usage_percent,
    storage_fast_provisioned_percent, storage_slow_provisioned_percent,
    vm_total, vcpu_provisioned
FROM datacenter
WHERE scope = 'PLATFORM'
ORDER BY data_coleta;

-- 2) capacityplan_daily_summary: consolidação diária (job daily_refresh da API, fora deste repositório)
-- ============================================================
-- Tabela própria do CapacityPlan: snapshot diário consolidado
-- (todas as plataformas + clientes + alertas de esgotamento).
-- Populada 1x/dia pelo CronJob capacityplan-daily-refresh (14h).
-- ============================================================
CREATE TABLE IF NOT EXISTS capacityplan_daily_summary (
    dia                     DATE PRIMARY KEY,
    cpu_usage_percent       NUMERIC(8,2),
    memory_usage_percent    NUMERIC(8,2),
    storage_usage_percent   NUMERIC(8,2),
    clientes_count          INTEGER,
    esgotamento_alertas     INTEGER,
    proximo_esgotamento     TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- storage_dc, storage_cliente_dc e nutanix_vms_dc são criadas pelo próprio coleta/storage_collect.py e coleta/nutanix_vms.py (constante SCHEMA).
-- tag_vcenter, tag_nutanix e tag_k8s são criadas pelos coletores coleta/*_tags.py.
