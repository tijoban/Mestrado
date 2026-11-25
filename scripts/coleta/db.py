"""Conexão com o banco canônico para storage_collect.py e nutanix_vms.py (POSTGRES_* do ambiente)."""
import os
from contextlib import contextmanager

import psycopg2


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()
