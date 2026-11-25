"""
Execução mensal da previsão de esgotamento (CronJob).

  python -m previsao.main

Para cada série de db_vcenter, db_nutanix (total, clusters, hosts) e
clientes_vmware (clientes), em duas variantes (bruto e estabilizado), treina
LSTM+GRU num subprocesso e grava em previsao_execucao / previsao_resultado /
previsao_ponto. Cada execução gera linhas novas; nada é apagado.
"""
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .db import conexao, executar, inserir_lote
from .fontes import series

# bruto = sem filtro; cnn e baseline = estabilização aplicada a todas as métricas.
VARIANTES = ("bruto", "cnn", "baseline")


def _log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _rodar_worker(tarefa):
    with tempfile.TemporaryDirectory() as tmp:
        entrada, saida = Path(tmp, "in.pkl"), Path(tmp, "out.pkl")
        entrada.write_bytes(pickle.dumps(tarefa))
        r = subprocess.run([sys.executable, "-m", "previsao.worker", str(entrada), str(saida)],
                           capture_output=True, text=True, timeout=6 * 3600,
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
        if saida.exists():
            return pickle.loads(saida.read_bytes())
        return {"erro": f"worker saiu com código {r.returncode}: {r.stderr[-500:]}"}


def _gravar(execucao_id, t, r):
    campos = ["n_pontos", "inicio_serie", "fim_serie", "ultimo_valor", "r2", "rmse", "nrmse", "mae",
              "mape", "epocas", "horizonte_lstm_dias", "max_previsto_lstm", "max_previsto_bruto", "divergencia",
              "tendencia_pp_dia",
              "data_esgotamento", "metodo_esgotamento", "duracao_seg", "erro"]
    (rid,) = executar(f"""
        INSERT INTO previsao_resultado
            (execucao_id, fonte, nivel, entidade, metrica, variante, estabilizacao, frequencia,
             limiar, {", ".join(campos)})
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, {", ".join(["%s"] * len(campos))})
        RETURNING id
    """, (execucao_id, t["fonte"], t["nivel"], t["entidade"], t["metrica"], t["variante"],
          t["variante"] if t["variante"] in ("cnn", "baseline") else "nenhuma",
          t["freq"], config.LIMIAR_PCT, *[r.get(c) for c in campos]), retorna=True)
    inserir_lote(
        "INSERT INTO previsao_ponto (resultado_id, dia, tipo, valor) VALUES %s ON CONFLICT DO NOTHING",
        [(rid, dia, tipo, valor) for dia, tipo, valor in r.get("pontos", [])])
    inserir_lote(
        "INSERT INTO previsao_avaliacao (resultado_id, referencia, r2, rmse, nrmse, mae, mape, n_teste) VALUES %s",
        [(rid, ref, a.get("r2"), a.get("rmse"), a.get("nrmse"), a.get("mae"), a.get("mape"), a.get("n_teste"))
         for ref, a in (r.get("avaliacao") or {}).items()])


def main():
    executar(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    tarefas = []
    for item in series():
        s = item.pop("serie")
        for variante in VARIANTES:
            tarefas.append({**item, "variante": variante,
                            "indice": [ts.isoformat() for ts in s.index], "valores": s.tolist()})
    if config.MAX_SERIES:
        tarefas = tarefas[:config.MAX_SERIES]

    parametros = {k: getattr(config, k) for k in (
        "LIMIAR_PCT", "LOOK_BACK", "HOLD_OUT", "EPOCAS", "HORIZONTE_LSTM_DIAS", "JANELA_TENDENCIA_DIAS",
        "HORIZONTE_MAX_ANOS", "FONTES", "NIVEIS", "NUTANIX_ATE", "NUTANIX_EXCLUIR", "TENDENCIA_ESTAVEL_PP_DIA")}
    parametros["modelo"] = "LSTM(64)+GRU(32)+Dense(1), adam/mse, EarlyStopping(10), ReduceLROnPlateau(5)"
    parametros["estabilizacao"] = ("variantes por série: bruto (sem filtro), cnn (IQR+CNN autoencoder) e "
                                   "baseline (IQR+MinMax), em cpu, memoria e disco; tela: cnn p/ cpu, baseline p/ memoria/disco")
    parametros["avaliacao"] = "hold-out 15%: métricas contra a série própria e contra os dados brutos (previsao_avaliacao)"
    ok = 0
    if config.RETOMAR:
        execucao_id = config.RETOMAR
        prontas = set()
        with conexao() as conn, conn.cursor() as cur:
            cur.execute("""SELECT fonte, nivel, entidade, metrica, variante FROM previsao_resultado
                           WHERE execucao_id = %s AND erro IS NULL""", (execucao_id,))
            prontas = {tuple(l) for l in cur.fetchall()}
            # resultados com erro são refeitos: apaga para não duplicar
            cur.execute("""DELETE FROM previsao_ponto WHERE resultado_id IN
                             (SELECT id FROM previsao_resultado WHERE execucao_id = %s AND erro IS NOT NULL)""",
                        (execucao_id,))
            cur.execute("""DELETE FROM previsao_avaliacao WHERE resultado_id IN
                             (SELECT id FROM previsao_resultado WHERE execucao_id = %s AND erro IS NOT NULL)""",
                        (execucao_id,))
            cur.execute("DELETE FROM previsao_resultado WHERE execucao_id = %s AND erro IS NOT NULL", (execucao_id,))
            conn.commit()
        ok = len(prontas)
        tarefas = [t for t in tarefas
                   if (t["fonte"], t["nivel"], t["entidade"], t["metrica"], t["variante"]) not in prontas]
        executar("""UPDATE previsao_execucao SET status = 'rodando', finalizado_em = NULL, erro = NULL,
                    series_total = %s, series_ok = %s WHERE id = %s""",
                 (ok + len(tarefas), ok, execucao_id))
        _log(f"retomando execução {execucao_id}: {ok} séries prontas, {len(tarefas)} a fazer, "
             f"{config.PARALELO} em paralelo")
    else:
        (execucao_id,) = executar(
            "INSERT INTO previsao_execucao (series_total, parametros) VALUES (%s, %s) RETURNING id",
            (len(tarefas), json.dumps(parametros)), retorna=True)
        _log(f"execução {execucao_id}: {len(tarefas)} séries, {config.PARALELO} em paralelo")

    try:
        with ThreadPoolExecutor(max_workers=config.PARALELO) as pool:
            futuros = {pool.submit(_rodar_worker, t): t for t in tarefas}
            for i, fut in enumerate(as_completed(futuros), 1):
                t = futuros[fut]
                r = fut.result()
                _gravar(execucao_id, t, r)
                nome = f"{t['fonte']}/{t['entidade']}/{t['metrica']}/{t['variante']}"
                if r.get("erro"):
                    _log(f"[{i}/{len(tarefas)}] {nome}: ERRO {r['erro']}")
                else:
                    ok += 1
                    executar("UPDATE previsao_execucao SET series_ok = %s WHERE id = %s", (ok, execucao_id))
                    _log(f"[{i}/{len(tarefas)}] {nome}: R2={r.get('r2')} esgota={r.get('data_esgotamento')} "
                         f"({r.get('metodo_esgotamento')}) {r.get('duracao_seg')}s")
        executar("UPDATE previsao_execucao SET status='ok', finalizado_em=now(), series_ok=%s WHERE id=%s",
                 (ok, execucao_id))
        _log(f"execução {execucao_id} concluída: {ok}/{len(tarefas)} séries")
    except Exception as e:
        executar("UPDATE previsao_execucao SET status='erro', finalizado_em=now(), erro=%s WHERE id=%s",
                 (f"{type(e).__name__}: {e}", execucao_id))
        raise


if __name__ == "__main__":
    main()
