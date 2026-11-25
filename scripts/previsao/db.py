import time
from contextlib import contextmanager
from functools import wraps

import psycopg2
import psycopg2.extras

from . import config


@contextmanager
def conexao():
    conn = psycopg2.connect(
        host=config.POSTGRES_HOST, port=config.POSTGRES_PORT,
        dbname=config.POSTGRES_DB, user=config.POSTGRES_USER,
        password=config.POSTGRES_PASSWORD, connect_timeout=15,
    )
    try:
        yield conn
    finally:
        conn.close()


def _com_retentativa(fn, tentativas=8, espera=30):
    """Repete a operação se a conexão cair (a execução 4 morreu num timeout do banco após 15 h)."""
    @wraps(fn)
    def f(*a, **kw):
        for i in range(1, tentativas + 1):
            try:
                return fn(*a, **kw)
            except psycopg2.OperationalError as e:
                if i == tentativas:
                    raise
                print(f"banco indisponível ({e.__class__.__name__}), tentativa {i}/{tentativas}; "
                      f"nova tentativa em {espera * i}s", flush=True)
                time.sleep(espera * i)
    return f


@_com_retentativa
def executar(sql, params=None, retorna=False):
    with conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            linha = cur.fetchone() if retorna else None
        conn.commit()
    return linha


@_com_retentativa
def inserir_lote(sql, linhas):
    if not linhas:
        return
    with conexao() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, linhas, page_size=2000)
        conn.commit()
