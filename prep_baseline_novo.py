"""
prep_baseline_novo.py
Lê db_nutanix e db_vcenter do PostgreSQL,
resamplea para 30min, aplica IQR + MinMaxScaler em cada entidade.
Salva: infra_baseline_novo.csv + tabela infra_baseline_novo.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
import psycopg2
from psycopg2.extras import execute_values
import env

# ──────────────────────────────────────────────────────────────────
TABLE_NUTANIX = "db_nutanix"
TABLE_VCENTER = "db_vcenter"
OUTPUT_CSV    = "infra_baseline_novo.csv"
TABLE_OUT     = "infra_baseline_novo"
FREQ          = "30min"
IQR_FACTOR    = 1.5
METRICAS      = ["cpu_percent", "mem_percent", "disk_percent"]

NUTANIX_ENTIDADES = [
    ("nutanix", "Total",      "ntx_total"),
    ("cluster", "ADAMANTIUM", "ntx_cluster_adamantium"),
    ("host",    "ntx001",     "ntx_host_ntx001"),
    ("host",    "ntx002",     "ntx_host_ntx002"),
    ("host",    "ntx003",     "ntx_host_ntx003"),
    ("host",    "ntx004",     "ntx_host_ntx004"),
]

VCENTER_ENTIDADES = [
    ("vcenter", "Total",             "vc_total"),
    ("cluster", "Cluster DIAMANTE",  "vc_cluster_diamante"),
    ("cluster", "Cluster TURMALINA", "vc_cluster_turmalina"),
    ("host", "vxr-esx-1.",  "vc_esx1"),
    ("host", "vxr-esx-2.",  "vc_esx2"),
    ("host", "vxr-esx-3.",  "vc_esx3"),
    ("host", "vxr-esx-4.",  "vc_esx4"),
    ("host", "vxr-esx-5.",  "vc_esx5"),
    ("host", "vxr-esx-6.",  "vc_esx6"),
    ("host", "vxr-esx-7.",  "vc_esx7"),
    ("host", "vxr-esx-8.",  "vc_esx8"),
    ("host", "vxr-esx-9.",  "vc_esx9"),
    ("host", "vxr-esx-10.", "vc_esx10"),
    ("host", "vxr-esx-11.", "vc_esx11"),
    ("host", "vxr-esx-12.", "vc_esx12"),
    ("host", "vxr-esx-13.", "vc_esx13"),
    ("host", "vxr-esx-14.", "vc_esx14"),
    ("host", "vxr-esx-15.", "vc_esx15"),
    ("host", "vxr-esx-16.", "vc_esx16"),
    ("host", "vxr-esx-17.", "vc_esx17"),
    ("host", "vxr-esx-18.", "vc_esx18"),
    ("host", "vxr-esx-19.", "vc_esx19"),
    ("host", "vxr-esx-20.", "vc_esx20"),
    ("host", "vxr-esx-21.", "vc_esx21"),
    ("host", "vxr-esx-22.", "vc_esx22"),
]


# ──────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(
        host=env.POSTGRES_HOST, port=env.POSTGRES_PORT,
        dbname=env.POSTGRES_DB, user=env.POSTGRES_USER,
        password=env.POSTGRES_PASSWORD,
    )


def ler_tabela(table_name):
    conn = get_conn()
    df = pd.read_sql(
        f"SELECT timestamp, level, name, "
        f"cpu_percent, mem_percent, disk_percent "
        f"FROM {table_name} ORDER BY timestamp",
        conn
    )
    conn.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    print(f"  {table_name}: {len(df)} registros lidos.")
    return df


def extrair_serie(df_raw, level, name, metrica,
                  prefixo, full_index):
    col_name = f"{prefixo}_{metrica}"
    sub = df_raw[
        (df_raw["level"] == level) &
        (df_raw["name"]  == name)
    ].copy()
    if sub.empty:
        print(f"    [AVISO] {level}/{name} não encontrado.")
        return pd.Series(np.nan, index=full_index, name=col_name)
    sub = sub.set_index("timestamp")[metrica]
    sub = pd.to_numeric(sub, errors="coerce")
    sub = sub.resample(FREQ).mean().reindex(full_index)
    sub.name = col_name
    return sub


def remove_outliers_iqr(s):
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    return s.where(
        (s >= q1 - IQR_FACTOR * iqr) &
        (s <= q3 + IQR_FACTOR * iqr)
    )


def stabilize(s):
    s = remove_outliers_iqr(s)
    s = s.interpolate(method="linear",
                      limit_direction="both").ffill().bfill()
    if s.isna().all():
        return s
    orig   = s.dropna()
    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(
        s.values.reshape(-1, 1)
    ).flatten()
    result = scaled * (orig.max() - orig.min()) + orig.min()
    return pd.Series(result, index=s.index, name=s.name)


def pg_type(series):
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMPTZ"
    elif pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    elif pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    return "TEXT"


def salvar_banco(df, table_name):
    conn = get_conn()
    cur  = conn.cursor()
    cols     = list(df.columns)
    cols_sql = ", ".join(f'"{c}"' for c in cols)
    col_defs = ", ".join(f'"{c}" {pg_type(df[c])}' for c in cols)
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    cur.execute(f"CREATE TABLE {table_name} ({col_defs})")
    rows = [
        tuple(None if (isinstance(v, float) and np.isnan(v))
              else v for v in row)
        for row in df.itertuples(index=False)
    ]
    execute_values(
        cur,
        f'INSERT INTO {table_name} ({cols_sql}) VALUES %s',
        rows, page_size=500
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  ✓ Tabela '{table_name}' criada ({len(df)} linhas).")


def main():
    print("Lendo dados do banco...")
    df_ntx = ler_tabela(TABLE_NUTANIX)
    df_vc  = ler_tabela(TABLE_VCENTER)

    all_ts = pd.concat([df_ntx["timestamp"], df_vc["timestamp"]])
    full_index = pd.date_range(
        start=all_ts.min().floor(FREQ),
        end  =all_ts.max().ceil(FREQ),
        freq =FREQ, tz="UTC"
    )
    print(f"\n  Período : {full_index[0]} → {full_index[-1]}")
    print(f"  Slots   : {len(full_index)}")

    series_list = []

    print("\n--- Nutanix ---")
    for level, name, prefixo in NUTANIX_ENTIDADES:
        for metrica in METRICAS:
            s = extrair_serie(df_ntx, level, name,
                              metrica, prefixo, full_index)
            print(f"  Estabilizando: {s.name}")
            series_list.append(stabilize(s))

    print("\n--- vCenter ---")
    for level, name, prefixo in VCENTER_ENTIDADES:
        for metrica in METRICAS:
            s = extrair_serie(df_vc, level, name,
                              metrica, prefixo, full_index)
            print(f"  Estabilizando: {s.name}")
            series_list.append(stabilize(s))

    df = pd.concat(series_list, axis=1)
    df.index.name = "timestamp"

    before = len(df)
    df.dropna(how="all", inplace=True)
    print(f"\n  Linhas all-NaN removidas : {before - len(df)}")
    print(f"  Shape final              : {df.shape}")

    df.to_csv(OUTPUT_CSV)
    print(f"  ✓ CSV: {OUTPUT_CSV}")
    salvar_banco(df.reset_index(), TABLE_OUT)


if __name__ == "__main__":
    main()
