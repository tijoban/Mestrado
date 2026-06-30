"""
previsao_novo.py
Lê infra_baseline_novo ou infra_cnn_novo do PostgreSQL,
treina LSTM/GRU por coluna e projeta 365 dias.
Cria tabelas previsao_baseline_novo / previsao_cnn_novo
        e      report_baseline_novo  / report_cnn_novo.

Uso:
  python previsao_novo.py --modo baseline
  python previsao_novo.py --modo cnn
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import warnings
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import (mean_absolute_error,
                             mean_squared_error, r2_score)
import psycopg2
from psycopg2.extras import execute_values
import env

import tensorflow as tf
from tensorflow.keras import layers, models, callbacks, optimizers

warnings.filterwarnings("ignore")
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

# ──────────────────────────────────────────────────────────────────
LOOK_BACK      = 672
FORECAST_STEPS = 17520
FREQ           = "30min"
TRAIN_RATIO    = 0.85
THRESHOLD      = 80.0

UNITS_1, UNITS_2 = 128, 64
DROPOUT          = 0.2
EPOCHS           = 80
BATCH_SIZE       = 64
PATIENCE         = 10
LR               = 1e-3


# ──────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(
        host=env.POSTGRES_HOST, port=env.POSTGRES_PORT,
        dbname=env.POSTGRES_DB, user=env.POSTGRES_USER,
        password=env.POSTGRES_PASSWORD,
    )


def ler_tabela_banco(table_name):
    """Lê tabela de séries estabilizadas do banco."""
    conn = get_conn()
    df = pd.read_sql(
        f"SELECT * FROM {table_name} ORDER BY timestamp",
        conn
    )
    conn.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")
    print(f"  {table_name}: {df.shape[0]} linhas × "
          f"{df.shape[1]} colunas lidas.")
    return df


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


# ──────────────────────────────────────────────────────────────────
def make_sequences(data, lb):
    X, y = [], []
    for i in range(len(data) - lb):
        X.append(data[i:i + lb])
        y.append(data[i + lb])
    return np.array(X), np.array(y)


def build_model(lb):
    inp = layers.Input(shape=(lb, 1))
    x   = layers.LSTM(UNITS_1, return_sequences=True)(inp)
    x   = layers.Dropout(DROPOUT)(x)
    x   = layers.GRU(UNITS_2)(x)
    x   = layers.Dropout(DROPOUT)(x)
    x   = layers.Dense(32, activation="relu")(x)
    out = layers.Dense(1)(x)
    m   = models.Model(inp, out)
    m.compile(optimizer=optimizers.Adam(LR),
              loss="huber", metrics=["mae"])
    return m


def recursive_forecast(model, last_window, steps, scaler):
    win   = last_window.copy()
    preds = []
    for _ in range(steps):
        p = model.predict(
            win.reshape(1, -1, 1), verbose=0
        )[0, 0]
        preds.append(p)
        win = np.append(win[1:], p)
    return scaler.inverse_transform(
        np.array(preds).reshape(-1, 1)
    ).flatten()


def exhaustion_date(fc, threshold):
    hit = fc[fc >= threshold]
    return (str(hit.index[0].date())
            if not hit.empty
            else "Não atingido em 365 dias")


def train_and_forecast(col, s):
    valid = s.dropna()
    if len(valid) < LOOK_BACK + 10:
        print(f"    [AVISO] {col}: {len(valid)} pontos "
              f"insuficientes, pulando.")
        return None, None

    vals   = valid.values.reshape(-1, 1)
    scaler = MinMaxScaler()
    normed = scaler.fit_transform(vals).flatten()

    X, y  = make_sequences(normed, LOOK_BACK)
    split = int(len(X) * TRAIN_RATIO)
    Xtr   = X[:split].reshape(-1, LOOK_BACK, 1)
    Xte   = X[split:].reshape(-1, LOOK_BACK, 1)
    ytr, yte = y[:split], y[split:]

    model = build_model(LOOK_BACK)
    model.fit(
        Xtr, ytr,
        epochs=EPOCHS, batch_size=BATCH_SIZE,
        validation_split=0.1,
        callbacks=[
            callbacks.EarlyStopping(
                patience=PATIENCE,
                restore_best_weights=True),
            callbacks.ReduceLROnPlateau(
                factor=0.5, patience=5, min_lr=1e-6),
        ],
        verbose=0,
    )

    yp_n = model.predict(Xte, verbose=0).flatten()
    yp   = scaler.inverse_transform(
        yp_n.reshape(-1, 1)
    ).flatten()
    yt   = scaler.inverse_transform(
        yte.reshape(-1, 1)
    ).flatten()

    mae  = mean_absolute_error(yt, yp)
    rmse = np.sqrt(mean_squared_error(yt, yp))
    mape = np.mean(
        np.abs((yt - yp) / np.where(yt == 0, 1, yt))
    ) * 100
    r2   = r2_score(yt, yp)
    print(f"    MAE={mae:.4f}  RMSE={rmse:.4f}  "
          f"MAPE={mape:.4f}%  R²={r2:.4f}")

    last_ts = valid.index[-1]
    future  = pd.date_range(
        last_ts + pd.Timedelta(minutes=30),
        periods=FORECAST_STEPS,
        freq=FREQ, tz=last_ts.tz,
    )
    fc_vals = recursive_forecast(
        model, normed[-LOOK_BACK:],
        FORECAST_STEPS, scaler
    )
    fc_vals = np.clip(fc_vals, 0, 100)
    fc_s    = pd.Series(fc_vals, index=future, name=col)

    report = {
        "coluna":           col,
        "limiar_pct":       THRESHOLD,
        "n_treino":         int(split),
        "n_teste":          int(len(yte)),
        "mae":              round(mae,  6),
        "rmse":             round(rmse, 6),
        "mape_pct":         round(mape, 6),
        "r2":               round(r2,   6),
        "max_previsto":     round(float(fc_vals.max()), 4),
        "min_previsto":     round(float(fc_vals.min()), 4),
        "media_prevista":   round(float(fc_vals.mean()), 4),
        "data_esgotamento": exhaustion_date(fc_s, THRESHOLD),
    }
    return fc_s, report


# ──────────────────────────────────────────────────────────────────
def main(modo):
    table_in   = f"infra_{modo}_novo"
    pred_csv   = f"previsao_{modo}_novo.csv"
    report_csv = f"report_{modo}_novo.csv"
    table_pred = f"previsao_{modo}_novo"
    table_rep  = f"report_{modo}_novo"

    print(f"Modo  : {modo.upper()}")
    print(f"Tabela de entrada: {table_in}\n")

    df = ler_tabela_banco(table_in)

    forecasts, reports = {}, []

    for col in df.columns:
        print(f"\n  [{col}]")
        fc, rpt = train_and_forecast(col, df[col])
        if fc is not None:
            forecasts[col] = fc
            reports.append(rpt)

    if not forecasts:
        print("[ERRO] Nenhuma coluna pôde ser prevista.")
        return

    df_pred            = pd.DataFrame(forecasts)
    df_pred.index.name = "timestamp"
    df_report          = pd.DataFrame(reports)

    # ── Salva CSVs ─────────────────────────────────────────────────
    df_pred.to_csv(pred_csv)
    df_report.to_csv(report_csv, index=False)
    print(f"\n✓ CSVs: {pred_csv}, {report_csv}")

    # ── Salva banco (tabelas _novo) ─────────────────────────────────
    salvar_banco(df_pred.reset_index(), table_pred)
    salvar_banco(df_report, table_rep)

    # ── Relatório resumido ──────────────────────────────────────────
    print(f"\n{'═'*72}")
    print(f"RELATÓRIO — {modo.upper()}")
    print(f"{'═'*72}")
    for metrica in ["cpu_percent", "mem_percent", "disk_percent"]:
        sub = df_report[
            df_report["coluna"].str.endswith(metrica)
        ]
        if sub.empty:
            continue
        print(f"\n  {metrica.upper()}")
        print(f"  {'Entidade':<35} {'R²':>8} "
              f"{'RMSE':>8} {'MAE':>8}  Esgotamento")
        print(f"  {'-'*75}")
        for _, row in sub.iterrows():
            ent = row["coluna"].replace(f"_{metrica}", "")
            print(f"  {ent:<35} "
                  f"{row['r2']:>8.4f} "
                  f"{row['rmse']:>8.4f} "
                  f"{row['mae']:>8.4f}  "
                  f"{row['data_esgotamento']}")

    # ── Alertas ────────────────────────────────────────────────────
    criticos = df_report[df_report["max_previsto"] >= 70.0]
    if not criticos.empty:
        print(f"\n⚠  Entidades com projeção acima de 70%:")
        for _, row in criticos.iterrows():
            print(f"   {row['coluna']}: "
                  f"máx={row['max_previsto']:.2f}%  "
                  f"esgot.={row['data_esgotamento']}")
    print(f"\n{'═'*72}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modo",
        choices=["baseline", "cnn"],
        default="baseline",
        help="baseline ou cnn"
    )
    args = parser.parse_args()
    main(args.modo)
