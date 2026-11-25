-- ============================================================
-- Previsão de esgotamento — CapacityPlan (TCC)
-- Cada execução mensal grava novas linhas; nada é apagado, para
-- que todas as rodadas fiquem disponíveis para a defesa.
-- ============================================================

CREATE TABLE IF NOT EXISTS previsao_execucao (
    id              SERIAL PRIMARY KEY,
    iniciado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizado_em   TIMESTAMPTZ,
    status          VARCHAR(16) NOT NULL DEFAULT 'rodando',  -- rodando | ok | erro
    series_total    INTEGER,
    series_ok       INTEGER NOT NULL DEFAULT 0,
    parametros      JSONB,
    erro            TEXT
);

-- Uma linha por série prevista (fonte × entidade × métrica × variante).
CREATE TABLE IF NOT EXISTS previsao_resultado (
    id                  BIGSERIAL PRIMARY KEY,
    execucao_id         INTEGER NOT NULL REFERENCES previsao_execucao(id),
    fonte               VARCHAR(16)  NOT NULL,   -- vcenter | nutanix | clientes
    nivel               VARCHAR(16)  NOT NULL,   -- total | cluster | host | cliente
    entidade            VARCHAR(128) NOT NULL,
    metrica             VARCHAR(16)  NOT NULL,   -- cpu | memoria | disco
    variante            VARCHAR(16)  NOT NULL,   -- bruto | estabilizado
    estabilizacao       VARCHAR(16)  NOT NULL,   -- nenhuma | cnn | baseline
    frequencia          VARCHAR(8)   NOT NULL,   -- 30min | 1D
    n_pontos            INTEGER,
    inicio_serie        TIMESTAMPTZ,
    fim_serie           TIMESTAMPTZ,
    ultimo_valor        DOUBLE PRECISION,        -- % no fim da série (após a variante)
    limiar              DOUBLE PRECISION,
    -- avaliação do modelo LSTM+GRU no hold-out
    r2                  DOUBLE PRECISION,
    rmse                DOUBLE PRECISION,
    nrmse               DOUBLE PRECISION,
    mae                 DOUBLE PRECISION,
    mape                DOUBLE PRECISION,
    epocas              INTEGER,
    -- projeção
    horizonte_lstm_dias INTEGER,
    max_previsto_lstm   DOUBLE PRECISION,
    tendencia_pp_dia    DOUBLE PRECISION,        -- inclinação (pontos percentuais/dia)
    data_esgotamento    TIMESTAMPTZ,
    metodo_esgotamento  VARCHAR(16),             -- lstm | tendencia | ja_acima | nao_esgota
    duracao_seg         DOUBLE PRECISION,
    erro                TEXT,
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prev_res_exec ON previsao_resultado (execucao_id, fonte, entidade);

-- Pontos diários para os gráficos: histórico (média do dia) e projeção.
CREATE TABLE IF NOT EXISTS previsao_ponto (
    resultado_id    BIGINT NOT NULL REFERENCES previsao_resultado(id),
    dia             DATE NOT NULL,
    tipo            VARCHAR(12) NOT NULL,        -- historico | lstm | tendencia
    valor           DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (resultado_id, tipo, dia)
);

-- Erro de cada previsão no trecho de teste (hold-out), em duas referências:
--   propria = contra a série que o modelo recebeu (bruta, CNN ou baseline)
--   bruta   = contra os dados brutos do mesmo período (compara as variantes)
CREATE TABLE IF NOT EXISTS previsao_avaliacao (
    resultado_id  BIGINT NOT NULL REFERENCES previsao_resultado(id),
    referencia    VARCHAR(8) NOT NULL,              -- propria | bruta
    r2            DOUBLE PRECISION,
    rmse          DOUBLE PRECISION,
    nrmse         DOUBLE PRECISION,
    mae           DOUBLE PRECISION,
    mape          DOUBLE PRECISION,
    n_teste       INTEGER,
    PRIMARY KEY (resultado_id, referencia)
);

-- "estabilizado" exibido = CNN para CPU, baseline para memória e disco
-- (execuções antigas gravavam a variante já como 'estabilizado').
-- Variante exibida na tela de Previsão: só a bruta, em CPU, memória e disco (o walk-forward mostrou que a
-- estabilização não melhora a previsão medida contra o dado real). As três variantes continuam gravadas.
CREATE OR REPLACE FUNCTION previsao_variante_tela(metrica TEXT, variante TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN variante = 'bruto' THEN 'bruto' END
$$;

-- Previsão limitada a [0, 100]% (worker.prever_recursivo); max_previsto_bruto = maior saída da rede antes da
-- trava, em %; divergencia = a rede tentou passar de 100% ou cair abaixo de 0%. Só acrescenta colunas.
ALTER TABLE previsao_resultado ADD COLUMN IF NOT EXISTS max_previsto_bruto DOUBLE PRECISION;
ALTER TABLE previsao_resultado ADD COLUMN IF NOT EXISTS divergencia BOOLEAN;

-- Visão prática para a defesa: última execução de cada fonte (uma fonte pode
-- ser reprocessada sozinha), bruto e estabilizado lado a lado, com o motivo
-- quando não há data de esgotamento.
CREATE OR REPLACE FUNCTION previsao_motivo(metodo TEXT, tendencia DOUBLE PRECISION)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN metodo IS NULL               THEN 'sem previsão'
        WHEN metodo = 'ja_acima'          THEN 'já acima do limiar'
        WHEN metodo IN ('lstm', 'tendencia') THEN 'esgota'
        WHEN tendencia < -0.01            THEN 'tendência de queda'
        WHEN tendencia <= 0.01            THEN 'estável'
        ELSE '> 10 anos'
    END
$$;

DROP VIEW IF EXISTS vw_previsao_esgotamento;
CREATE VIEW vw_previsao_esgotamento AS
WITH ult AS (
    SELECT r.fonte, max(r.execucao_id) AS id
    FROM previsao_resultado r JOIN previsao_execucao e ON e.id = r.execucao_id
    WHERE e.status = 'ok' AND NOT coalesce((e.parametros ->> 'teste_local')::boolean, false)
    GROUP BY r.fonte
)
SELECT r.execucao_id, r.fonte, r.nivel, r.entidade, r.metrica,
       max(r.data_esgotamento)   FILTER (WHERE r.variante_tela = 'bruto')        AS esgotamento_bruto,
       max(r.metodo_esgotamento) FILTER (WHERE r.variante_tela = 'bruto')        AS metodo_bruto,
       max(previsao_motivo(r.metodo_esgotamento, r.tendencia_pp_dia)) FILTER (WHERE r.variante_tela = 'bruto') AS motivo_bruto,
       max(r.r2)                 FILTER (WHERE r.variante_tela = 'bruto')        AS r2_bruto,
       max(r.nrmse)              FILTER (WHERE r.variante_tela = 'bruto')        AS nrmse_bruto,
       max(r.data_esgotamento)   FILTER (WHERE r.variante_tela = 'estabilizado') AS esgotamento_estabilizado,
       max(r.metodo_esgotamento) FILTER (WHERE r.variante_tela = 'estabilizado') AS metodo_estabilizado,
       max(previsao_motivo(r.metodo_esgotamento, r.tendencia_pp_dia)) FILTER (WHERE r.variante_tela = 'estabilizado') AS motivo_estabilizado,
       max(r.r2)                 FILTER (WHERE r.variante_tela = 'estabilizado') AS r2_estabilizado,
       max(r.nrmse)              FILTER (WHERE r.variante_tela = 'estabilizado') AS nrmse_estabilizado,
       max(r.ultimo_valor)       FILTER (WHERE r.variante_tela = 'bruto')        AS ultimo_valor,
       max(r.tendencia_pp_dia)   FILTER (WHERE r.variante_tela = 'bruto')        AS tendencia_pp_dia,
       max(r.fim_serie)          FILTER (WHERE r.variante_tela = 'bruto')        AS fim_serie
FROM (SELECT r0.*, previsao_variante_tela(r0.metrica, r0.variante) AS variante_tela FROM previsao_resultado r0) r
JOIN ult ON r.execucao_id = ult.id AND r.fonte = ult.fonte
WHERE r.variante_tela IS NOT NULL
GROUP BY r.execucao_id, r.fonte, r.nivel, r.entidade, r.metrica;

-- Experimento (qualificação): bruto x CNN x baseline em todas as métricas,
-- com o erro medido contra os dados brutos e contra a própria série.
CREATE OR REPLACE VIEW vw_previsao_avaliacao AS
SELECT r.execucao_id, r.fonte, r.nivel, r.entidade, r.metrica, r.variante, r.estabilizacao,
       a.referencia, a.r2, a.rmse, a.nrmse, a.mae, a.mape, a.n_teste,
       r.data_esgotamento, previsao_motivo(r.metodo_esgotamento, r.tendencia_pp_dia) AS motivo
FROM previsao_resultado r
JOIN previsao_avaliacao a ON a.resultado_id = r.id;

-- Resumo por fonte/métrica/variante (mediana é robusta a séries quase
-- constantes, onde R² e MAPE explodem).
CREATE OR REPLACE VIEW vw_previsao_avaliacao_resumo AS
SELECT execucao_id, fonte, metrica, variante, referencia, count(*) AS series,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r2)    AS r2_mediano,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY rmse)  AS rmse_mediano,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY nrmse) AS nrmse_mediano,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY mae)   AS mae_mediano,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY mape)  AS mape_mediano,
       avg(rmse) AS rmse_medio, avg(mae) AS mae_medio
FROM vw_previsao_avaliacao
GROUP BY execucao_id, fonte, metrica, variante, referencia;
