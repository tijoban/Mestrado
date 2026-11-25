"""
Processa UMA série em subprocesso isolado (memória do TensorFlow liberada ao
sair), no mesmo padrão de _defesa_train_worker.py:

  1. variante: bruto (só preenche lacunas) ou estabilizado (CNN/baseline)
  2. LSTM(64) + GRU(32) treinado com hold-out de 15% -> R², RMSE, NRMSE, MAE, MAPE
  3. previsão recursiva por HORIZONTE_LSTM_DIAS
  4. depois disso, a curva segue pela tendência linear da série até cruzar o
     limiar -> data de esgotamento (ou "nao_esgota" se a tendência não cresce)

Uso interno: python -m previsao.worker entrada.pkl saida.pkl
"""
import os
import pickle
import sys
import time
from datetime import timedelta

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")

import numpy as np
import pandas as pd

from . import config
from .estabilizacao import estabilizar

PONTOS_DIA = {"30min": 48, "1D": 1}
PASSO = {"30min": timedelta(minutes=30), "1D": timedelta(days=1)}


def prever_recursivo(m, janela, passos, t0, passo):
    """Previsão recursiva da rede (série em fração de 0 a 1) por `passos` passos a partir de t0.

    Percentual não sai de [0, 100]: a saída da rede é limitada a [0, 1] antes de ser gravada e antes de voltar
    para a janela da recursão. Devolve (previsão em %, maior saída bruta em %, divergencia), em que divergencia
    indica que a rede tentou passar de 100% ou cair abaixo de 0% (ex.: memória do Cluster B, 164,7% na
    execução 5 de nov/2026).
    """
    lb = len(janela)
    janela = np.asarray(janela, dtype="float32").copy()
    previstos, bruto_min, bruto_max = [], np.inf, -np.inf
    for i in range(1, passos + 1):
        nxt = float(m(janela.reshape(1, lb, 1), training=False).numpy()[0, 0])
        bruto_min, bruto_max = min(bruto_min, nxt), max(bruto_max, nxt)
        nxt = min(max(nxt, 0.0), 1.0)
        previstos.append((t0 + passo * i, nxt * 100))
        janela = np.append(janela[1:], nxt)
    prev = pd.Series([v for _, v in previstos], index=pd.DatetimeIndex([ts for ts, _ in previstos]))
    return prev, bruto_max * 100, bool(bruto_max > 1.0 or bruto_min < 0.0)


def _sequencias(vals, lb):
    X = np.array([vals[i:i + lb] for i in range(len(vals) - lb)])
    return X.reshape(-1, lb, 1), vals[lb:]


def _configurar_tf():
    """Threads do TF: precisa ser antes de qualquer uso (a CNN da estabilização
    também usa o TensorFlow)."""
    import tensorflow as tf
    tf.config.threading.set_intra_op_parallelism_threads(2)
    tf.config.threading.set_inter_op_parallelism_threads(2)


def _modelo(lb):
    from tensorflow.keras import layers, models
    m = models.Sequential([
        layers.Input(shape=(lb, 1)),
        layers.LSTM(64, return_sequences=True),
        layers.GRU(32),
        layers.Dense(1),
    ])
    m.compile(optimizer="adam", loss="mse")
    return m


def _metricas(y, p):
    rmse = float(np.sqrt(np.mean((y - p) ** 2)))
    media = float(np.mean(y))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return {
        "rmse": rmse,
        "mae": float(np.mean(np.abs(y - p))),
        "mape": float(np.mean(np.abs((y - p) / np.where(np.abs(y) < 1e-6, 1e-6, y))) * 100),
        "nrmse": rmse / media if abs(media) > 1e-6 else None,
        "r2": (1 - float(np.sum((y - p) ** 2)) / ss_tot) if ss_tot > 1e-10 else None,
    }


def processar(t):
    _configurar_tf()
    from tensorflow.keras import callbacks

    inicio = time.time()
    freq, metrica, variante = t["freq"], t["metrica"], t["variante"]
    s = pd.Series(t["valores"], index=pd.DatetimeIndex(t["indice"]))
    s = s.loc[s.first_valid_index():s.last_valid_index()]

    # Série bruta (só lacunas preenchidas): referência para comparar variantes.
    bruta = s.interpolate(method="time", limit_direction="both").ffill().bfill()
    if variante in ("cnn", "baseline"):
        s = estabilizar(s, variante)
    else:
        s = bruta

    out = {"n_pontos": int(s.notna().sum()),
           "inicio_serie": s.index[0].to_pydatetime(), "fim_serie": s.index[-1].to_pydatetime()}
    if out["n_pontos"] < config.MIN_PONTOS[freq]:
        out["erro"] = f"histórico insuficiente ({out['n_pontos']} pontos)"
        return out

    lb, limiar = config.LOOK_BACK[freq], config.LIMIAR_PCT
    vals = s.values.astype(float) / 100.0          # percentual -> fração para o treino
    corte = int(len(vals) * (1 - config.HOLD_OUT))
    Xtr, ytr = _sequencias(vals[:corte], lb)
    Xte, yte = _sequencias(np.concatenate([vals[corte - lb:corte], vals[corte:]]), lb)

    m = _modelo(lb)
    h = m.fit(Xtr, ytr, epochs=config.EPOCAS, batch_size=64, validation_split=0.1, verbose=0, callbacks=[
        callbacks.EarlyStopping(patience=10, restore_best_weights=True),
        callbacks.ReduceLROnPlateau(factor=0.5, patience=5, min_lr=1e-6),
    ])
    pred = m.predict(Xte, verbose=0).flatten() * 100
    out.update(_metricas(yte * 100, pred))
    out["epocas"] = len(h.history["loss"])
    # Avaliação no mesmo trecho de teste: contra a série que o modelo viu
    # ("propria") e contra os dados brutos ("bruta") — só a segunda permite
    # comparar bruto, CNN e baseline entre si.
    y_bruto = bruta.reindex(s.index).values.astype(float)[corte:]
    out["avaliacao"] = {
        "propria": {**_metricas(yte * 100, pred), "n_teste": len(yte)},
        "bruta": {**_metricas(y_bruto, pred), "n_teste": len(y_bruto)},
    }

    # ── previsão recursiva LSTM+GRU ──
    passos = config.HORIZONTE_LSTM_DIAS * PONTOS_DIA[freq]
    prev, out["max_previsto_bruto"], out["divergencia"] = prever_recursivo(m, vals[-lb:], passos, s.index[-1], PASSO[freq])
    out["horizonte_lstm_dias"] = config.HORIZONTE_LSTM_DIAS
    out["max_previsto_lstm"] = float(prev.max())

    # ── tendência (pontos percentuais por dia) nos últimos N dias ──
    diario = s.resample("1D").mean().dropna()
    recente = diario.loc[diario.index >= diario.index[-1] - timedelta(days=config.JANELA_TENDENCIA_DIAS)]
    x = (recente.index - recente.index[0]).days.values.astype(float)
    inclinacao = float(np.polyfit(x, recente.values, 1)[0]) if len(recente) >= 7 else 0.0
    out["tendencia_pp_dia"] = inclinacao
    out["ultimo_valor"] = float(diario.iloc[-1])

    # ── data de esgotamento ──
    pontos_tend = []
    if out["ultimo_valor"] >= limiar:
        out["metodo_esgotamento"], out["data_esgotamento"] = "ja_acima", s.index[-1].to_pydatetime()
    elif (prev >= limiar).any():
        out["metodo_esgotamento"] = "lstm"
        out["data_esgotamento"] = prev[prev >= limiar].index[0].to_pydatetime()
    elif inclinacao > 1e-6:
        v0, d0 = float(prev.iloc[-1]), prev.index[-1]
        dias = (limiar - v0) / inclinacao
        if dias <= config.HORIZONTE_MAX_ANOS * 365:
            out["metodo_esgotamento"] = "tendencia"
            out["data_esgotamento"] = (d0 + timedelta(days=dias)).to_pydatetime()
            # linha crescente até o limiar (um ponto por semana + o ponto final)
            for k in list(np.arange(7, dias, 7)) + [dias]:
                pontos_tend.append(((d0 + timedelta(days=float(k))).date(), v0 + inclinacao * float(k)))
        else:
            out["metodo_esgotamento"], out["data_esgotamento"] = "nao_esgota", None
    else:
        out["metodo_esgotamento"], out["data_esgotamento"] = "nao_esgota", None

    # ── pontos diários para o gráfico ──
    hist = diario.iloc[-config.DIAS_HISTORICO_GRAFICO:]
    out["pontos"] = (
        [(d.date(), "historico", float(v)) for d, v in hist.items()]
        + [(d.date(), "lstm", float(v)) for d, v in prev.resample("1D").mean().dropna().items()]
        + [(d, "tendencia", float(v)) for d, v in pontos_tend]
    )
    out["duracao_seg"] = round(time.time() - inicio, 1)
    return out


if __name__ == "__main__":
    entrada, saida = sys.argv[1], sys.argv[2]
    with open(entrada, "rb") as f:
        tarefa = pickle.load(f)
    try:
        resultado = processar(tarefa)
    except Exception as e:  # o processo principal registra o erro da série
        resultado = {"erro": f"{type(e).__name__}: {e}"}
    with open(saida, "wb") as f:
        pickle.dump(resultado, f)
