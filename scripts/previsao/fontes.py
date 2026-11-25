"""Lê as séries do banco e devolve uma por (fonte, entidade, métrica)."""
import pandas as pd

from . import config
from .db import conexao

METRICAS = {"cpu": "cpu_percent", "memoria": "mem_percent", "disco": "disk_percent"}
NIVEL = {"vcenter": "total", "nutanix": "total", "cluster": "cluster", "host": "host"}


def _plataforma(fonte):
    tabela = {"vcenter": "db_vcenter", "nutanix": "db_nutanix"}[fonte]
    with conexao() as conn:
        df = pd.read_sql(
            f"SELECT timestamp, level, name, cpu_percent, mem_percent, disk_percent "
            f"FROM {tabela} ORDER BY timestamp", conn)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if fonte == "nutanix" and config.NUTANIX_ATE:
        fim = pd.Timestamp(config.NUTANIX_ATE, tz="America/Recife") + pd.Timedelta(days=1)
        df = df[df["timestamp"] < fim]
    for (level, name), sub in df.groupby(["level", "name"]):
        nivel = NIVEL.get(level, level)
        if nivel not in config.NIVEIS:
            continue
        if fonte == "nutanix" and name in config.NUTANIX_EXCLUIR:
            continue
        sub = sub.set_index("timestamp").sort_index()
        for metrica, coluna in METRICAS.items():
            s = pd.to_numeric(sub[coluna], errors="coerce").resample("30min").mean()
            yield {"fonte": fonte, "nivel": nivel, "entidade": name,
                   "metrica": metrica, "freq": "30min", "serie": s}


def _clientes():
    if "cliente" not in config.NIVEIS:
        return
    # Percentual do que está alocado para o cliente (pastas "Orgao" no vCenter).
    with conexao() as conn:
        df = pd.read_sql("""
            SELECT data_coleta, nome,
                   cpu_usage_mhz * 100.0 / NULLIF(cpu_capacity_mhz, 0)   AS cpu,
                   memory_usage_mb * 100.0 / NULLIF(memory_total_mb, 0)  AS memoria,
                   disk_usage_gb * 100.0 / NULLIF(disk_total_gb, 0)      AS disco
            FROM clientes_vmware
            WHERE tipo = 'folder'
            ORDER BY data_coleta
        """, conn)
    df["data_coleta"] = pd.to_datetime(df["data_coleta"]).dt.tz_localize("America/Recife").dt.tz_convert("UTC")
    for nome, sub in df.groupby("nome"):
        sub = sub.set_index("data_coleta").sort_index()
        for metrica in METRICAS:
            s = pd.to_numeric(sub[metrica], errors="coerce").resample("1D").mean()
            yield {"fonte": "clientes", "nivel": "cliente", "entidade": nome,
                   "metrica": metrica, "freq": "1D", "serie": s}


def series():
    for fonte in config.FONTES:
        if fonte in ("vcenter", "nutanix"):
            yield from _plataforma(fonte)
        elif fonte == "clientes":
            yield from _clientes()
