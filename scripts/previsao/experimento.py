"""
Experimento de avaliação (dissertação) — walk-forward sem vazamento de dados.

  python -m previsao.experimento                    # nova rodada, todas as séries
  RETOMAR=<id> python -m previsao.experimento       # continua a rodada <id>
  MAX_SERIES=3 python -m previsao.experimento       # teste rápido

Responde às recomendações da banca de qualificação:
  * janela x horizonte: previsão de H=30 dias por janela, sobre séries diárias
    com 5 a 9 meses de histórico;
  * walk-forward expansivo: K janelas; em cada uma TODO o pré-processamento
    (limites do IQR, autoencoder 1D-CNN e escala MinMax das redes) é ajustado só
    com o treino da janela, e a previsão é multi-passo recursiva (o modelo não
    vê nenhum ponto do teste); o erro é medido contra a série BRUTA do teste;
  * métricas imunes a séries planas: MASE e sMAPE, além de MAE, RMSE, NRMSE e R²;
  * fallback de séries planas (servidores ociosos): variação desprezível no
    fim do treino -> projeção constante, marcada como "plana";
  * picos: além da média diária, o percentil 95 diário (P95) das séries de
    30 min, e o erro do pico previsto na janela;
  * comparação com modelos simples: naive, naive sazonal (7 dias), regressão
    linear, Holt amortecido (ETS) e ARIMA(1,1,1), contra a LSTM+GRU e a CNN-LSTM
    (convolução treinada de ponta a ponta com a rede, como na literatura);
  * estabilização medida à parte, no treino: quanto cada variante distorce a
    série bruta (MAE, retenção do pico e do P95, variância removida, outliers).

Grava em previsao_wf_execucao / previsao_wf_resultado / previsao_wf_ponto /
previsao_wf_estabilizacao (nunca apaga: cada rodada tem o seu execucao_id).
O job mensal (previsao.main) não muda: este módulo só mede a qualidade da previsão.
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

import numpy as np
import pandas as pd

from . import config

H = int(os.environ.get("WF_HORIZONTE", "30"))          # dias previstos por janela
K_MAX = int(os.environ.get("WF_JANELAS", "4"))           # janelas do walk-forward
TREINO_MIN = int(os.environ.get("WF_TREINO_MIN", "60"))  # dias mínimos na 1ª janela
TESTE_MIN_OBS = 15       # dias observados mínimos numa janela de teste (lacunas de coleta)
LOOK_BACK = 14                                          # dias de entrada das redes
PLANA_DESVIO = 0.05      # p.p.: desvio-padrão dos últimos 30 dias de treino
PLANA_AMPLITUDE = 0.5    # p.p.: amplitude dos últimos 30 dias de treino
VARIANTES = ("bruto", "baseline", "cnn")                 # baseline = IQR + interpolação (+ MinMax)
MODELOS = ("naive", "sazonal7", "linear", "ets", "arima", "lstm", "cnn_lstm")
PARALELO = int(os.environ.get("WF_PARALELO", "3"))
NIVEIS_PONTOS = ("total", "cluster")  # pontos previstos gravados só destes níveis


def _log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _dur(seg):
    seg = int(seg)
    return f"{seg // 3600}h{(seg % 3600) // 60:02d}m" if seg >= 3600 else f"{seg // 60}m{seg % 60:02d}s"


# ───────────────────────── séries diárias e janelas ─────────────────────────
def diaria(s, agregacao):
    """Média ou P95 diário, com NaN nos dias sem coleta (ex.: 14/07–10/08/2026)."""
    s = s.loc[s.first_valid_index():s.last_valid_index()]
    d = s.resample("1D").mean() if agregacao == "media" else s.resample("1D").quantile(0.95)
    return d.loc[d.first_valid_index():d.last_valid_index()]


def janelas(valores):
    """[(fold, origem)] das janelas válidas: K = min(K_MAX, (n - TREINO_MIN) // H), cada uma com H dias de
    teste no fim da série; descarta a janela com menos de TESTE_MIN_OBS dias observados no teste."""
    v = np.asarray([np.nan if x is None else x for x in valores], dtype=float)
    n = len(v)
    k = min(K_MAX, (n - TREINO_MIN) // H)
    saida = []
    for f in range(max(k, 0)):
        origem = n - (k - f) * H
        if (~np.isnan(v[origem:origem + H])).sum() >= TESTE_MIN_OBS:
            saida.append((f + 1, origem))
    return saida, k


def _preencher_treino(x):
    """Lacunas do treino: interpolação linear entre pontos do próprio treino."""
    return pd.Series(x).interpolate(limit_direction="both").values


# ───────────────────────── pré-processamento (só treino) ─────────────────────────
def _sem_outliers(x):
    """IQR 1,5 no treino; os pontos fora viram NaN e são interpolados. Devolve (série, máscara dos alterados)."""
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    dentro = (x >= q1 - 1.5 * iqr) & (x <= q3 + 1.5 * iqr)
    y = pd.Series(np.where(dentro, x, np.nan))
    return y.interpolate(limit_direction="both").ffill().bfill().values, ~dentro


def _cnn(x):
    """Autoencoder 1D-CNN: MinMax do próprio treino, reconstrução por janelas sobrepostas, volta à escala."""
    from tensorflow.keras import callbacks, layers, models
    lo, hi = x.min(), x.max()
    n = len(x)
    w = max(8, min(28, (n // 4) // 2 * 2))
    if hi - lo < 1e-9 or n < w + 1:
        return x
    norm = (x - lo) / (hi - lo)
    X = np.array([norm[i:i + w] for i in range(n - w + 1)]).reshape(-1, w, 1)
    inp = layers.Input(shape=(w, 1))
    h = layers.Conv1D(32, 5, activation="relu", padding="same")(inp)
    h = layers.MaxPooling1D(2)(h)
    h = layers.Conv1D(16, 5, activation="relu", padding="same")(h)
    h = layers.UpSampling1D(2)(h)
    h = layers.Conv1D(32, 5, activation="relu", padding="same")(h)
    out = layers.Conv1D(1, 1, activation="sigmoid")(h)
    m = models.Model(inp, out)
    m.compile(optimizer="adam", loss="mse")
    m.fit(X, X, epochs=30, batch_size=32, validation_split=0.1, verbose=0,
          callbacks=[callbacks.EarlyStopping(patience=5, restore_best_weights=True)])
    rec = m.predict(X, verbose=0).reshape(-1, w)
    soma, cont = np.zeros(n), np.zeros(n)
    for i, p in enumerate(rec):
        soma[i:i + w] += p
        cont[i:i + w] += 1
    return soma / np.maximum(cont, 1) * (hi - lo) + lo


def preprocessar(treino, variante):
    """(série estabilizada na escala original, máscara dos pontos alterados pelo IQR)."""
    if variante == "bruto":
        return treino.copy(), np.zeros(len(treino), dtype=bool)
    x, alterados = _sem_outliers(treino)
    return (_cnn(x) if variante == "cnn" else x), alterados


def metricas_estab(bruta, est, alterados):
    """Quanto a variante distorceu a série bruta no treino (p.p.)."""
    d = est - bruta
    p95b = float(np.percentile(bruta, 95))
    varb = float(np.var(bruta))
    return {
        "mae_estab": float(np.mean(np.abs(d))),
        "retencao_pico": float(est.max() / bruta.max()) if abs(bruta.max()) > 1e-9 else None,
        "retencao_p95": float(np.percentile(est, 95) / p95b) if abs(p95b) > 1e-9 else None,
        "variancia_removida": float(1 - np.var(est) / varb) if varb > 1e-12 else None,
        "outliers_removidos": float(np.mean(alterados) * 100),
        "vies_estab": float(np.mean(d)),                 # > 0: a série estabilizada ficou acima da bruta
        "vies_borda7": float(np.mean(d[-7:])),           # nos 7 dias de onde a previsão parte
    }


# ───────────────────────── modelos ─────────────────────────
def prever(modelo, x, h):
    n = len(x)
    if modelo == "naive":
        return np.repeat(x[-1], h)
    if modelo == "sazonal7":
        return np.array([x[n - 7 + (i % 7)] for i in range(h)])
    if modelo == "linear":
        y = x[-90:]
        a, b = np.polyfit(np.arange(len(y)), y, 1)
        return a * np.arange(len(y), len(y) + h) + b
    if modelo == "ets":
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        return np.asarray(ExponentialSmoothing(x, trend="add", damped_trend=True,
                                               initialization_method="estimated").fit().forecast(h))
    if modelo == "arima":
        from statsmodels.tsa.arima.model import ARIMA
        return np.asarray(ARIMA(x, order=(1, 1, 1)).fit().forecast(h))
    if modelo in ("lstm", "cnn_lstm"):
        return _rede(x, h, modelo)
    raise ValueError(modelo)


def _rede(x, h, arquitetura):
    """LSTM(64)+GRU(32)+Dense(1) ou CNN-LSTM (Conv1D(32,k=3) -> LSTM(64) -> Dense(1)), mesma entrada:
    MinMax ajustado só no treino, janela de LOOK_BACK dias, previsão recursiva desnormalizada pelo mesmo scaler."""
    from tensorflow.keras import callbacks, layers, models
    lo, hi = x.min(), x.max()
    esc = (hi - lo) or 1.0
    z = (x - lo) / esc
    X = np.array([z[i:i + LOOK_BACK] for i in range(len(z) - LOOK_BACK)]).reshape(-1, LOOK_BACK, 1)
    y = z[LOOK_BACK:]
    if arquitetura == "lstm":
        camadas = [layers.LSTM(64, return_sequences=True), layers.GRU(32), layers.Dense(1)]
    else:
        camadas = [layers.Conv1D(32, 3, activation="relu", padding="causal"), layers.LSTM(64), layers.Dense(1)]
    m = models.Sequential([layers.Input(shape=(LOOK_BACK, 1)), *camadas])
    m.compile(optimizer="adam", loss="mse")
    m.fit(X, y, epochs=config.EPOCAS, batch_size=16, validation_split=0.1, verbose=0, callbacks=[
        callbacks.EarlyStopping(patience=10, restore_best_weights=True),
        callbacks.ReduceLROnPlateau(factor=0.5, patience=5, min_lr=1e-6)])
    janela, prev = z[-LOOK_BACK:].astype("float32").copy(), []
    for _ in range(h):
        nxt = float(m(janela.reshape(1, LOOK_BACK, 1), training=False).numpy()[0, 0])
        prev.append(nxt)
        janela = np.append(janela[1:], nxt)
    return np.array(prev) * esc + lo


# ───────────────────────── métricas ─────────────────────────
def metricas(y, p, treino_bruto):
    """y = série BRUTA do teste (dias observados); p = previsão na escala original."""
    e = p - y
    mae = float(np.mean(np.abs(e)))
    rmse = float(np.sqrt(np.mean(e ** 2)))
    den = np.abs(y) + np.abs(p)
    smape = float(np.mean(np.where(den < 1e-9, 0.0, 2 * np.abs(e) / np.where(den < 1e-9, 1, den))) * 100)
    escala = float(np.mean(np.abs(np.diff(treino_bruto))))   # MAE do naive (1 passo) no treino bruto
    var = float(np.sum((y - y.mean()) ** 2))
    media = float(np.mean(y))
    return {
        "mae": mae,
        "rmse": rmse,
        "nrmse": rmse / media if abs(media) > 1e-6 else None,
        "smape": smape,
        "mase": mae / escala if escala > 1e-9 else None,
        "r2": 1 - float(np.sum(e ** 2)) / var if var / max(len(y), 1) > 1e-6 else None,
        "erro_pico": float(p.max() - y.max()),     # < 0: subestimou o pico da janela
        "vies": float(np.mean(e)),
    }


# ───────────────────────── uma série (subprocesso) ─────────────────────────
def processar(t):
    """Uma série diária: todas as janelas × variantes × modelos que ainda não estão gravados (t['feitos'])."""
    feitos = {tuple(x) for x in t.get("feitos", [])}            # (variante, modelo, fold) já gravados
    feitos_estab = {tuple(x) for x in t.get("feitos_estab", [])}  # (variante, fold)
    y = pd.Series([np.nan if v is None else v for v in t["valores"]],
                  index=pd.DatetimeIndex(t["indice"])).astype(float)
    n = len(y)
    js, k = janelas(t["valores"])
    if k < 1:
        return {"erro": f"histórico insuficiente ({n} dias)"}
    if not js:
        return {"erro": f"nenhuma janela com {TESTE_MIN_OBS}+ dias observados"}
    precisa_tf = any((v, m, f) not in feitos for f, _ in js for v in VARIANTES for m in ("lstm", "cnn_lstm")) or \
        any((v, f) not in feitos_estab for f, _ in js for v in ("cnn",))
    if precisa_tf:
        import tensorflow as tf
        tf.config.threading.set_intra_op_parallelism_threads(1)
        tf.config.threading.set_inter_op_parallelism_threads(1)

    linhas, pontos, estab = [], [], []
    valores = y.values
    for fold, origem in js:
        treino = _preencher_treino(valores[:origem])
        teste_cal = valores[origem:origem + H]            # dias de calendário (com NaN)
        obs = ~np.isnan(teste_cal)
        teste = teste_cal[obs]
        cauda = treino[-30:]
        plana = bool(np.std(cauda) < PLANA_DESVIO and np.ptp(cauda) < PLANA_AMPLITUDE)
        # referência da razão: o naive sobre a série bruta (o último valor observado)
        mae_naive = float(np.mean(np.abs(np.repeat(treino[-1], len(teste_cal))[obs] - teste)))
        for variante in VARIANTES:
            faltam = [m for m in MODELOS if (variante, m, fold) not in feitos]
            mede_estab = variante != "bruto" and not plana and (variante, fold) not in feitos_estab
            if not faltam and not mede_estab:
                continue
            if plana:
                x = treino
            else:
                x, alterados = preprocessar(treino, variante)
                if mede_estab:
                    estab.append({"variante": variante, "fold": fold, "n_treino": origem,
                                  "teste_acima_max": bool(teste.max() > treino.max()),
                                  "excesso_teste": float(teste.max() - treino.max()),
                                  **metricas_estab(treino, x, alterados)})
            for modelo in faltam:
                ini = time.time()
                try:
                    pc = np.repeat(treino[-1], len(teste_cal)) if plana else prever(modelo, x, len(teste_cal))
                    if not np.all(np.isfinite(pc)):
                        raise ValueError("previsão não finita")
                    p = pc[obs]                                   # avalia só os dias observados
                    met, erro = metricas(teste, p, treino), None
                    met["divergencia"] = bool(pc.max() > 100 or pc.min() < 0)   # saída fora de [0, 100]%
                    met["razao_naive"] = met["mae"] / mae_naive if mae_naive > 1e-9 else None
                    met["vence_naive"] = bool(met["mae"] < mae_naive) if mae_naive > 1e-9 else None
                except Exception as e:  # um modelo que não converge não derruba a série
                    p, met, erro = None, {}, f"{type(e).__name__}: {e}"[:300]
                linhas.append({"variante": variante, "modelo": modelo, "fold": fold,
                               "origem": y.index[origem].date(), "n_treino": origem, "h": len(teste),
                               "plana": plana, "erro": erro, "duracao_seg": round(time.time() - ini, 2), **met})
                if p is not None and t["nivel"] in NIVEIS_PONTOS:
                    dias = y.index[origem:origem + H][obs]
                    pontos += [(variante, modelo, fold, dias[i].date(), float(teste[i]), float(p[i]))
                               for i in range(len(teste))]
    return {"linhas": linhas, "pontos": pontos, "estab": estab, "n_dias": n, "janelas": len(js), "k_teorico": k}


# ───────────────────────── orquestração ─────────────────────────
def _tarefas():
    from .fontes import series
    tarefas = []
    for item in series():
        s = item.pop("serie")
        aggs = ("media", "p95") if item["freq"] == "30min" else ("media",)
        for agregacao in aggs:
            d = diaria(s, agregacao)
            tarefas.append({**item, "agregacao": agregacao,
                            "indice": [ts.isoformat() for ts in d.index],
                            "valores": [None if pd.isna(v) else float(v) for v in d]})
    return tarefas


def _rodar(t):
    with tempfile.TemporaryDirectory() as tmp:
        ent, sai = Path(tmp, "in.pkl"), Path(tmp, "out.pkl")
        ent.write_bytes(pickle.dumps(t))
        r = subprocess.run([sys.executable, "-m", "previsao.experimento", "--worker", str(ent), str(sai)],
                           capture_output=True, text=True, timeout=3 * 3600,
                           env={**os.environ, "PYTHONUNBUFFERED": "1", "TF_CPP_MIN_LOG_LEVEL": "3"})
        if sai.exists():
            return pickle.loads(sai.read_bytes())
        return {"erro": f"worker saiu com código {r.returncode}: {r.stderr[-400:]}"}


COLS = ["variante", "modelo", "fold", "origem", "n_treino", "h", "plana", "mae", "rmse", "nrmse", "smape",
        "mase", "r2", "erro_pico", "vies", "razao_naive", "vence_naive", "divergencia", "erro", "duracao_seg"]
COLS_ESTAB = ["variante", "fold", "n_treino", "mae_estab", "retencao_pico", "retencao_p95", "variancia_removida",
              "outliers_removidos", "vies_estab", "vies_borda7", "teste_acima_max", "excesso_teste"]


def _gravar(eid, t, r):
    from .db import executar, inserir_lote
    chave = (eid, t["fonte"], t["nivel"], t["entidade"], t["metrica"], t["agregacao"])
    if r.get("erro"):
        executar("""INSERT INTO previsao_wf_resultado (execucao_id, fonte, nivel, entidade, metrica, agregacao,
                    variante, modelo, fold, erro) VALUES (%s,%s,%s,%s,%s,%s,'-','-',0,%s)
                    ON CONFLICT DO NOTHING""", (*chave, r["erro"]))
        return
    inserir_lote(f"""INSERT INTO previsao_wf_resultado (execucao_id, fonte, nivel, entidade, metrica, agregacao,
                     {", ".join(COLS)}) VALUES %s ON CONFLICT DO NOTHING""",
                 [(*chave, *[l.get(c) for c in COLS]) for l in r["linhas"]])
    inserir_lote(f"""INSERT INTO previsao_wf_estabilizacao (execucao_id, fonte, nivel, entidade, metrica, agregacao,
                     {", ".join(COLS_ESTAB)}) VALUES %s ON CONFLICT DO NOTHING""",
                 [(*chave, *[l.get(c) for c in COLS_ESTAB]) for l in r.get("estab", [])])
    executar("""INSERT INTO previsao_wf_serie (execucao_id, fonte, nivel, entidade, metrica, agregacao, n_dias,
                k_teorico, k_efetivo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
             (*chave, r["n_dias"], r.get("k_teorico"), r["janelas"]))
    inserir_lote("""INSERT INTO previsao_wf_ponto (execucao_id, fonte, entidade, metrica, agregacao,
                    variante, modelo, fold, dia, real, previsto) VALUES %s ON CONFLICT DO NOTHING""",
                 [(eid, t["fonte"], t["entidade"], t["metrica"], t["agregacao"], *p) for p in r["pontos"]])


def _feitos(eid):
    """O que a rodada já tem gravado: {série: {(variante, modelo, fold)}} e {série: {(variante, fold)}}.
    A linha de erro da série inteira (variante '-') não conta: a série é tentada de novo."""
    from .db import conexao
    res, est = {}, {}
    with conexao() as conn, conn.cursor() as cur:
        cur.execute("""SELECT fonte, entidade, metrica, agregacao, variante, modelo, fold FROM previsao_wf_resultado
                       WHERE execucao_id = %s AND variante <> '-'""", (eid,))
        for f, e, m, a, v, mo, fo in cur.fetchall():
            res.setdefault((f, e, m, a), set()).add((v, mo, fo))
        cur.execute("""SELECT fonte, entidade, metrica, agregacao, variante, fold FROM previsao_wf_estabilizacao
                       WHERE execucao_id = %s""", (eid,))
        for f, e, m, a, v, fo in cur.fetchall():
            est.setdefault((f, e, m, a), set()).add((v, fo))
    return res, est


def _completa(t, feitos, feitos_estab):
    js, _ = janelas(t["valores"])
    if not js:
        return False
    return all((v, m, f) in feitos for f, _ in js for v in VARIANTES for m in MODELOS)


def main():
    from .db import executar
    executar(Path(__file__).with_name("experimento_schema.sql").read_text(encoding="utf-8"))
    tarefas = _tarefas()
    if config.MAX_SERIES:
        tarefas = tarefas[:config.MAX_SERIES]
    parametros = {"H": H, "K_MAX": K_MAX, "TREINO_MIN": TREINO_MIN, "LOOK_BACK": LOOK_BACK,
                  "PLANA_DESVIO": PLANA_DESVIO, "PLANA_AMPLITUDE": PLANA_AMPLITUDE, "TESTE_MIN_OBS": TESTE_MIN_OBS,
                  "lacunas": "sem preenchimento; treino interpolado só com pontos do treino; teste avaliado só em "
                             "dias observados",
                  "erro": "contra a série bruta do teste; razao_naive = MAE / MAE do naive sobre a série bruta",
                  "VARIANTES": VARIANTES, "MODELOS": MODELOS, "EPOCAS": config.EPOCAS,
                  "NUTANIX_ATE": config.NUTANIX_ATE, "NUTANIX_EXCLUIR": config.NUTANIX_EXCLUIR,
                  "FONTES": config.FONTES, "NIVEIS": config.NIVEIS, "MAX_SERIES": config.MAX_SERIES}
    retomar = int(os.environ.get("RETOMAR", "0"))
    if retomar:
        eid = retomar
        executar("UPDATE previsao_wf_execucao SET status='rodando', finalizado_em=NULL WHERE id=%s", (eid,))
    else:
        (eid,) = executar("""INSERT INTO previsao_wf_execucao (series_total, parametros) VALUES (%s, %s)
                             RETURNING id""", (len(tarefas), json.dumps(parametros)), retorna=True)
    estado = os.environ.get("WF_ESTADO")
    if estado:
        Path(estado).write_text(json.dumps({"execucao": eid, "status": "rodando"}), encoding="utf-8")
    feitos, feitos_estab = _feitos(eid)
    total = len(tarefas)
    for t in tarefas:
        ch = (t["fonte"], t["entidade"], t["metrica"], t["agregacao"])
        t["feitos"] = sorted(feitos.get(ch, set()))
        t["feitos_estab"] = sorted(feitos_estab.get(ch, set()))
    prontas = [t for t in tarefas if _completa(t, set(map(tuple, t["feitos"])), None)]
    tarefas = [t for t in tarefas if t not in prontas]
    _log(f"experimento {eid}: {total} séries diárias, {len(prontas)} já completas (puladas), "
         f"{len(tarefas)} a processar, {PARALELO} em paralelo")
    ok = erros = 0
    ini = time.time()
    try:
        with ThreadPoolExecutor(max_workers=PARALELO) as pool:
            fut = {pool.submit(_rodar, t): t for t in tarefas}
            for i, f in enumerate(as_completed(fut), 1):
                t, r = fut[f], f.result()
                _gravar(eid, t, r)
                nome = f"{t['fonte']}/{t['nivel']}/{t['entidade']}/{t['metrica']}/{t['agregacao']}"
                dec = time.time() - ini
                resto = dec / i * (len(tarefas) - i)
                prog = f"[{i}/{len(tarefas)}] decorrido {_dur(dec)}, estimado {_dur(resto)}"
                if r.get("erro"):
                    erros += 1
                    _log(f"{prog} {nome}: ERRO {r['erro']}")
                else:
                    ok += 1
                    executar("UPDATE previsao_wf_execucao SET series_ok = series_ok + 1 WHERE id = %s", (eid,))
                    _log(f"{prog} {nome}: {r['n_dias']} dias, {r['janelas']} janelas, {len(r['linhas'])} resultados")
        executar("UPDATE previsao_wf_execucao SET status='ok', finalizado_em=now() WHERE id=%s", (eid,))
        if estado:
            Path(estado).write_text(json.dumps({"execucao": eid, "status": "ok"}), encoding="utf-8")
        _log(f"experimento {eid} concluído em {_dur(time.time() - ini)}: {ok} ok, {erros} com erro, "
             f"{len(prontas)} puladas (já completas)")
    except Exception as e:
        executar("UPDATE previsao_wf_execucao SET status='erro', finalizado_em=now(), erro=%s WHERE id=%s",
                 (f"{type(e).__name__}: {e}", eid))
        raise


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        tarefa = pickle.loads(Path(sys.argv[2]).read_bytes())
        try:
            res = processar(tarefa)
        except Exception as e:
            res = {"erro": f"{type(e).__name__}: {e}"}
        Path(sys.argv[3]).write_bytes(pickle.dumps(res))
    else:
        main()
