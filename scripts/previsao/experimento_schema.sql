-- Experimento de avaliação walk-forward (previsao.experimento). Nunca DROP.
CREATE TABLE IF NOT EXISTS previsao_wf_execucao (
    id              SERIAL PRIMARY KEY,
    iniciado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizado_em   TIMESTAMPTZ,
    status          VARCHAR(16) NOT NULL DEFAULT 'rodando',
    series_total    INTEGER,
    series_ok       INTEGER NOT NULL DEFAULT 0,
    parametros      JSONB,
    erro            TEXT
);

-- Uma linha por série diária × variante × modelo × janela.
CREATE TABLE IF NOT EXISTS previsao_wf_resultado (
    execucao_id  INTEGER NOT NULL REFERENCES previsao_wf_execucao(id),
    fonte        VARCHAR(16)  NOT NULL,
    nivel        VARCHAR(16)  NOT NULL,
    entidade     VARCHAR(128) NOT NULL,
    metrica      VARCHAR(16)  NOT NULL,   -- cpu | memoria | disco
    agregacao    VARCHAR(8)   NOT NULL,   -- media | p95 (diário)
    variante     VARCHAR(16)  NOT NULL,   -- bruto | baseline | cnn
    modelo       VARCHAR(16)  NOT NULL,   -- naive | sazonal7 | linear | ets | arima | lstm
    fold         SMALLINT     NOT NULL,
    origem       DATE,
    n_treino     INTEGER,
    h            INTEGER,
    plana        BOOLEAN,
    mae          DOUBLE PRECISION,
    rmse         DOUBLE PRECISION,
    smape        DOUBLE PRECISION,
    mase         DOUBLE PRECISION,
    r2           DOUBLE PRECISION,
    erro_pico    DOUBLE PRECISION,
    vies         DOUBLE PRECISION,
    erro         TEXT,
    duracao_seg  DOUBLE PRECISION,
    PRIMARY KEY (execucao_id, fonte, entidade, metrica, agregacao, variante, modelo, fold)
);

CREATE TABLE IF NOT EXISTS previsao_wf_ponto (
    execucao_id  INTEGER NOT NULL REFERENCES previsao_wf_execucao(id),
    fonte        VARCHAR(16)  NOT NULL,
    entidade     VARCHAR(128) NOT NULL,
    metrica      VARCHAR(16)  NOT NULL,
    agregacao    VARCHAR(8)   NOT NULL,
    variante     VARCHAR(16)  NOT NULL,
    modelo       VARCHAR(16)  NOT NULL,
    fold         SMALLINT     NOT NULL,
    dia          DATE         NOT NULL,
    real         DOUBLE PRECISION,
    previsto     DOUBLE PRECISION,
    PRIMARY KEY (execucao_id, fonte, entidade, metrica, agregacao, variante, modelo, fold, dia)
);

-- Rodada completa (nov/2026): métricas novas, sem mexer nas linhas antigas.
ALTER TABLE previsao_wf_resultado ADD COLUMN IF NOT EXISTS nrmse DOUBLE PRECISION;      -- RMSE / média do teste
ALTER TABLE previsao_wf_resultado ADD COLUMN IF NOT EXISTS razao_naive DOUBLE PRECISION; -- MAE / MAE do naive (série bruta)
ALTER TABLE previsao_wf_resultado ADD COLUMN IF NOT EXISTS vence_naive BOOLEAN;
ALTER TABLE previsao_wf_resultado ADD COLUMN IF NOT EXISTS divergencia BOOLEAN;         -- previsão fora de [0, 100]%

-- Quanto cada variante distorce a série bruta, medido no TREINO de cada janela (p.p.).
CREATE TABLE IF NOT EXISTS previsao_wf_estabilizacao (
    execucao_id         INTEGER NOT NULL REFERENCES previsao_wf_execucao(id),
    fonte               VARCHAR(16)  NOT NULL,
    nivel               VARCHAR(16)  NOT NULL,
    entidade            VARCHAR(128) NOT NULL,
    metrica             VARCHAR(16)  NOT NULL,
    agregacao           VARCHAR(8)   NOT NULL,
    variante            VARCHAR(16)  NOT NULL,   -- baseline (IQR) | cnn
    fold                SMALLINT     NOT NULL,
    n_treino            INTEGER,
    mae_estab           DOUBLE PRECISION,        -- MAE entre estabilizada e bruta
    retencao_pico       DOUBLE PRECISION,        -- max(estabilizada) / max(bruta)
    retencao_p95        DOUBLE PRECISION,        -- P95(estabilizada) / P95(bruta)
    variancia_removida  DOUBLE PRECISION,        -- 1 - var(estabilizada) / var(bruta)
    outliers_removidos  DOUBLE PRECISION,        -- % dos pontos alterados pelo IQR
    vies_estab          DOUBLE PRECISION,        -- média(estabilizada - bruta)
    vies_borda7         DOUBLE PRECISION,        -- idem nos últimos 7 dias do treino (de onde a previsão parte)
    teste_acima_max     BOOLEAN,                 -- o teste passa do máximo do treino?
    excesso_teste       DOUBLE PRECISION,        -- max(teste) - max(treino)
    PRIMARY KEY (execucao_id, fonte, entidade, metrica, agregacao, variante, fold)
);

-- Seleção de modelo por série (só avaliação): escolhido pelo menor MASE médio nas janelas 1..K-1,
-- avaliado só na janela K, ao lado da LSTM+GRU e do ETS na mesma janela.
CREATE TABLE IF NOT EXISTS previsao_wf_selecao (
    execucao_id      INTEGER NOT NULL REFERENCES previsao_wf_execucao(id),
    fonte            VARCHAR(16)  NOT NULL,
    nivel            VARCHAR(16)  NOT NULL,
    entidade         VARCHAR(128) NOT NULL,
    metrica          VARCHAR(16)  NOT NULL,
    agregacao        VARCHAR(8)   NOT NULL,
    variante         VARCHAR(16)  NOT NULL,
    fold_k           SMALLINT     NOT NULL,
    modelo_escolhido VARCHAR(16)  NOT NULL,
    mase_sel         DOUBLE PRECISION,
    mae_sel          DOUBLE PRECISION,
    mase_lstm        DOUBLE PRECISION,
    mase_ets         DOUBLE PRECISION,
    mase_naive       DOUBLE PRECISION,
    PRIMARY KEY (execucao_id, fonte, entidade, metrica, agregacao, variante)
);

-- Por série: dias de histórico, janelas possíveis (K teórico) e janelas válidas (K efetivo). A diferença são
-- as janelas descartadas por terem menos de 15 dias observados no teste (lacuna de coleta de jul–ago/2026).
CREATE TABLE IF NOT EXISTS previsao_wf_serie (
    execucao_id  INTEGER NOT NULL REFERENCES previsao_wf_execucao(id),
    fonte        VARCHAR(16)  NOT NULL,
    nivel        VARCHAR(16)  NOT NULL,
    entidade     VARCHAR(128) NOT NULL,
    metrica      VARCHAR(16)  NOT NULL,
    agregacao    VARCHAR(8)   NOT NULL,
    n_dias       INTEGER,
    k_teorico    SMALLINT,
    k_efetivo    SMALLINT,
    PRIMARY KEY (execucao_id, fonte, entidade, metrica, agregacao)
);
