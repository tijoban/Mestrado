"""
Estabilização das séries — mesma lógica de defesa_estabilizacao_banco.py:
  - CPU            : IQR + 1D-CNN autoencoder
  - Memória / disco: IQR + interpolação + MinMax (baseline)
Importa o TensorFlow só quando a CNN é usada (roda dentro do worker).
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler

IQR_FACTOR = 1.5
CNN_WINDOW, CNN_FILTERS, CNN_KERNEL = 48, 64, 5
CNN_EPOCHS, CNN_BATCH, CNN_PATIENCE = 30, 256, 5


def metodo_para(metrica):
    return "cnn" if metrica == "cpu" else "baseline"


def _sem_outliers(s):
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    return s.where((s >= q1 - IQR_FACTOR * iqr) & (s <= q3 + IQR_FACTOR * iqr))


def _interpolada(s):
    return s.interpolate(method="linear", limit_direction="both").ffill().bfill()


def baseline(s):
    s = _interpolada(_sem_outliers(s))
    if s.isna().all() or s.std() < 1e-8:
        return s
    lo, hi = s.min(), s.max()
    escalada = MinMaxScaler().fit_transform(s.values.reshape(-1, 1)).flatten()
    return pd.Series(escalada * (hi - lo) + lo, index=s.index)


def _cnn_autoencoder(window):
    from tensorflow.keras import layers, models
    inp = layers.Input(shape=(window, 1))
    x = layers.Conv1D(CNN_FILTERS, CNN_KERNEL, activation="relu", padding="same")(inp)
    x = layers.Conv1D(CNN_FILTERS // 2, CNN_KERNEL, activation="relu", padding="same")(x)
    x = layers.MaxPooling1D(2)(x)
    x = layers.Conv1D(CNN_FILTERS // 4, CNN_KERNEL, activation="relu", padding="same")(x)
    x = layers.UpSampling1D(2)(x)
    x = layers.Conv1D(CNN_FILTERS // 2, CNN_KERNEL, activation="relu", padding="same")(x)
    x = layers.Conv1D(CNN_FILTERS, CNN_KERNEL, activation="relu", padding="same")(x)
    out = layers.Conv1D(1, 1, activation="sigmoid")(x)
    m = models.Model(inp, out)
    m.compile(optimizer="adam", loss="mse")
    return m


def cnn(s):
    from tensorflow.keras import callbacks
    s = _interpolada(_sem_outliers(s))
    vals = s.values.astype(float)
    lo, hi = vals.min(), vals.max()
    n = len(vals)
    window = CNN_WINDOW if n >= CNN_WINDOW * 4 else max(8, (n // 4) // 2 * 2)
    if hi == lo or n < window + 1:
        return baseline(s)
    norm = (vals - lo) / (hi - lo)
    X = np.array([norm[i:i + window] for i in range(n - window + 1)]).reshape(-1, window, 1)
    m = _cnn_autoencoder(window)
    m.fit(X, X, epochs=CNN_EPOCHS, batch_size=CNN_BATCH, validation_split=0.1, verbose=0,
          callbacks=[callbacks.EarlyStopping(patience=CNN_PATIENCE, restore_best_weights=True)])
    pred = m.predict(X, verbose=0).reshape(-1, window)
    soma, cont = np.zeros(n), np.zeros(n)
    for i, p in enumerate(pred):
        soma[i:i + window] += p
        cont[i:i + window] += 1
    return pd.Series(soma / np.where(cont == 0, 1, cont) * (hi - lo) + lo, index=s.index)


def estabilizar(s, metodo):
    """metodo = "cnn" ou "baseline" — aplicado a qualquer métrica (experimento:
    CNN tende a servir séries muito variáveis; baseline, séries estáveis)."""
    return cnn(s) if metodo == "cnn" else baseline(s)
