"""
Análise do experimento walk-forward (dissertação).

  EXECUCAO=<id> SAIDA=resultados_wf ANON_MAPA=<csv fora do repo> python -m previsao.analise_wf

Lê previsao_wf_resultado / previsao_wf_estabilizacao da rodada, grava a seleção de modelo por série em
previsao_wf_selecao (só avaliação) e gera RESULTADOS_WALKFORWARD.md, CSVs e figuras em SAIDA.
Os nomes de clusters, hosts e clientes saem anonimizados; o mapeamento real fica em ANON_MAPA, fora do
repositório. Nenhuma senha, token ou string de conexão vai para os arquivos gerados.
"""
import json, os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from .db import conexao, inserir_lote

EID = int(os.environ["EXECUCAO"])
SAIDA = Path(os.environ.get("SAIDA", "resultados_wf"))
ANON_MAPA = Path(os.environ.get("ANON_MAPA", "anonimizacao_wf.csv"))
VAR = {"bruto": "bruta", "baseline": "iqr", "cnn": "cnn"}
MOD = {"naive": "naive", "sazonal7": "naive sazonal (7d)", "linear": "regressão linear", "ets": "Holt amortecido (ETS)",
       "arima": "ARIMA(1,1,1)", "lstm": "LSTM+GRU", "cnn_lstm": "CNN-LSTM", "selecao_por_serie": "seleção por série"}
MET = {"cpu": "CPU", "memoria": "memória", "disco": "disco"}
SIMPLES = ("naive", "sazonal7", "linear", "ets", "arima")
NIVEL_CLASSE = {"total": "agregado", "cluster": "granular", "host": "granular", "cliente": "cliente"}


def ler(sql, params=None):
    with conexao() as conn:
        return pd.read_sql(sql, conn, params=params)


# ───────────────────────── anonimização ─────────────────────────
FIXOS = json.loads(os.environ.get("ANONIMIZAR_FIXOS", "{}"))  # {"nome real": "nome anônimo"}, fora do repositório
# ex.: ANONIMIZAR_FIXOS='{"Cluster X": "Cluster A", "Cluster Y": "Cluster B"}'


def mapa_anon(df):
    """Real -> anônimo, ordem fixa (alfabética dentro de cada grupo); gravado fora do repositório."""
    if ANON_MAPA.exists():
        m = pd.read_csv(ANON_MAPA)
        mapa = dict(zip(m["real"], m["anonimo"]))
    else:
        mapa = {}
    ents = df[["fonte", "nivel", "entidade"]].drop_duplicates()
    for (fonte, nivel), g in ents.groupby(["fonte", "nivel"]):
        for i, e in enumerate(sorted(g["entidade"]), 1):
            if e in mapa:
                continue
            if e in FIXOS:
                mapa[e] = FIXOS[e]
            elif nivel == "total":
                mapa[e] = "vCenter (total)" if fonte == "vcenter" else "Nutanix (total)"
            elif nivel == "host":
                mapa[e] = f"Host {'V' if fonte == 'vcenter' else 'N'}{i:02d}"
            elif nivel == "cliente":
                mapa[e] = f"Cliente {i:02d}"
            else:
                mapa[e] = f"{nivel} {i:02d}"
    ANON_MAPA.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sorted(mapa.items()), columns=["real", "anonimo"]).to_csv(ANON_MAPA, index=False)
    return mapa


# ───────────────────────── utilitários ─────────────────────────
def md(df, casas=2):
    """DataFrame -> tabela markdown (sem depender do tabulate)."""
    def f(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else f"{v:.{casas}f}".replace(".", ",")
        return str(v)
    cab = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    return "\n".join([cab, sep] + ["| " + " | ".join(f(v) for v in l) + " |" for l in df.itertuples(index=False)])


def holm(ps):
    ps = np.asarray(ps, dtype=float)
    ordem = np.argsort(ps)
    m = len(ps)
    adj = np.empty(m)
    acum = 0.0
    for r, i in enumerate(ordem):
        acum = max(acum, min(1.0, (m - r) * ps[i]))
        adj[i] = acum
    return adj


def wilcoxon(a, b):
    """Teste pareado sobre a - b (MASE): n, mediana da diferença, p e correlação rank-biserial."""
    d = np.asarray(a, float) - np.asarray(b, float)
    d = d[np.isfinite(d)]
    nz = d[d != 0]
    if len(nz) < 6:
        return {"n": len(d), "mediana_dif": float(np.median(d)) if len(d) else np.nan, "p": np.nan, "r_rb": np.nan}
    p = float(stats.wilcoxon(nz).pvalue)
    ranks = stats.rankdata(np.abs(nz))
    wp, wm = ranks[nz > 0].sum(), ranks[nz < 0].sum()
    return {"n": len(d), "mediana_dif": float(np.median(d)), "p": p, "r_rb": float((wp - wm) / (wp + wm))}


# ───────────────────────── carga ─────────────────────────
def carregar():
    r = ler("""SELECT * FROM previsao_wf_resultado WHERE execucao_id = %s""", (EID,))
    falhas = r[r["variante"] == "-"]
    r = r[(r["variante"] != "-") & r["erro"].isna()].copy()
    e = ler("SELECT * FROM previsao_wf_estabilizacao WHERE execucao_id = %s", (EID,))
    ex = ler("SELECT * FROM previsao_wf_execucao WHERE id = %s", (EID,))
    return r, e, falhas, ex


def selecao(r):
    """Por série × variante × agregação com 2+ janelas: escolhe o menor MASE médio nas janelas 1..K-1 e mede
    só na janela K. Grava em previsao_wf_selecao e devolve as linhas."""
    chave = ["fonte", "nivel", "entidade", "metrica", "agregacao", "variante"]
    saida = []
    for k, g in r[r["mase"].notna()].groupby(chave):
        folds = sorted(g["fold"].unique())
        if len(folds) < 2:
            continue
        fk = folds[-1]
        antes = g[g["fold"] < fk].groupby("modelo")["mase"].agg(["mean", "count"])
        antes = antes[antes["count"] == len(folds) - 1]
        ultima = g[g["fold"] == fk].set_index("modelo")
        if antes.empty:
            continue
        esc = antes["mean"].idxmin()
        if esc not in ultima.index:
            continue
        val = lambda m, c="mase": float(ultima.loc[m, c]) if m in ultima.index else None
        saida.append({**dict(zip(chave, k)), "fold_k": int(fk), "modelo_escolhido": esc, "mase_sel": val(esc),
                      "mae_sel": val(esc, "mae"), "mase_lstm": val("lstm"), "mase_ets": val("ets"),
                      "mase_naive": val("naive")})
    if saida:
        cols = chave + ["fold_k", "modelo_escolhido", "mase_sel", "mae_sel", "mase_lstm", "mase_ets", "mase_naive"]
        inserir_lote(f"""INSERT INTO previsao_wf_selecao (execucao_id, {", ".join(cols)}) VALUES %s
                         ON CONFLICT (execucao_id, fonte, entidade, metrica, agregacao, variante) DO UPDATE SET
                         fold_k = EXCLUDED.fold_k, modelo_escolhido = EXCLUDED.modelo_escolhido,
                         mase_sel = EXCLUDED.mase_sel, mae_sel = EXCLUDED.mae_sel, mase_lstm = EXCLUDED.mase_lstm,
                         mase_ets = EXCLUDED.mase_ets, mase_naive = EXCLUDED.mase_naive""",
                     [(EID, *[s[c] for c in cols]) for s in saida])
    return pd.DataFrame(saida)


# ───────────────────────── tabelas ─────────────────────────
def t1(r):
    g = r.groupby(["agregacao", "metrica", "variante", "modelo"])
    t = g.agg(n=("mase", "size"), MAE=("mae", "median"), sMAPE=("smape", "median"), MASE=("mase", "median"),
              NRMSE=("nrmse", "median"), vence_naive=("vence_naive", "mean"), erro_pico=("erro_pico", "median")).reset_index()
    t["vence_naive"] = t["vence_naive"] * 100
    return t.sort_values(["agregacao", "metrica", "variante", "MASE"])


def t2(e):
    t = e.groupby(["agregacao", "metrica", "variante"]).agg(
        n=("mae_estab", "size"), mae_estab=("mae_estab", "median"), retencao_pico=("retencao_pico", "median"),
        retencao_p95=("retencao_p95", "median"), variancia_removida=("variancia_removida", "median"),
        outliers_pct=("outliers_removidos", "median"), vies_borda7=("vies_borda7", "median"),
        teste_acima_max_pct=("teste_acima_max", "mean")).reset_index()
    t["teste_acima_max_pct"] *= 100
    return t


def pares(r, filtro_a, filtro_b):
    chave = ["fonte", "entidade", "metrica", "agregacao", "fold"]
    a = r.query(filtro_a)[chave + ["mase"]]
    b = r.query(filtro_b)[chave + ["mase"]]
    m = a.merge(b, on=chave, suffixes=("_a", "_b")).dropna()
    return m["mase_a"].values, m["mase_b"].values


def t3(r):
    rr = r[~r["plana"].fillna(False)]
    linhas = []
    for (agg, met), g in rr.groupby(["agregacao", "metrica"]):
        simples = g[(g["variante"] == "bruto") & g["modelo"].isin(SIMPLES)].groupby("modelo")["mase"].median()
        melhor = simples.idxmin() if len(simples) else None
        testes = [("cnn × iqr", "LSTM+GRU", "variante=='cnn' and modelo=='lstm'", "variante=='baseline' and modelo=='lstm'"),
                  ("iqr × bruta", "LSTM+GRU", "variante=='baseline' and modelo=='lstm'", "variante=='bruto' and modelo=='lstm'"),
                  ("cnn × bruta", "LSTM+GRU", "variante=='cnn' and modelo=='lstm'", "variante=='bruto' and modelo=='lstm'")]
        if melhor:
            q = f"modelo=='{melhor}'"
            testes += [("cnn × iqr", MOD[melhor], f"variante=='cnn' and {q}", f"variante=='baseline' and {q}"),
                       ("iqr × bruta", MOD[melhor], f"variante=='baseline' and {q}", f"variante=='bruto' and {q}"),
                       ("cnn × bruta", MOD[melhor], f"variante=='cnn' and {q}", f"variante=='bruto' and {q}")]
        testes += [("LSTM+GRU × ETS", "bruta", "variante=='bruto' and modelo=='lstm'", "variante=='bruto' and modelo=='ets'"),
                   ("LSTM+GRU × ARIMA", "bruta", "variante=='bruto' and modelo=='lstm'", "variante=='bruto' and modelo=='arima'"),
                   ("CNN-LSTM × LSTM+GRU", "bruta", "variante=='bruto' and modelo=='cnn_lstm'", "variante=='bruto' and modelo=='lstm'")]
        for nome, contexto, fa, fb in testes:
            a, b = pares(g, fa, fb)
            linhas.append({"agregacao": agg, "metrica": met, "comparacao": nome, "com": contexto, **wilcoxon(a, b)})
    t = pd.DataFrame(linhas)
    if len(t):
        ok = t["p"].notna()
        t.loc[ok, "p_holm"] = holm(t.loc[ok, "p"].values)
        t["aviso"] = np.where(t["n"] < 10, "n < 10", "")
    return t


def t4(sel):
    if sel.empty:
        return pd.DataFrame(), pd.DataFrame()
    s = sel.dropna(subset=["mase_sel"])
    resumo = s.groupby(["agregacao", "metrica", "variante"])[["mase_sel", "mase_lstm", "mase_ets"]].apply(lambda g: pd.Series({
        "n": len(g), "MASE seleção": g["mase_sel"].median(), "MASE LSTM+GRU": g["mase_lstm"].median(),
        "MASE ETS": g["mase_ets"].median(), "seleção < LSTM (%)": (g["mase_sel"] < g["mase_lstm"]).mean() * 100,
        "seleção < ETS (%)": (g["mase_sel"] < g["mase_ets"]).mean() * 100})).reset_index()
    resumo["n"] = resumo["n"].astype(int)
    freq = (s.groupby(["metrica", "modelo_escolhido"]).size().rename("vezes").reset_index())
    freq["%"] = freq["vezes"] / freq.groupby("metrica")["vezes"].transform("sum") * 100
    freq["modelo_escolhido"] = freq["modelo_escolhido"].map(MOD)
    return resumo, freq.sort_values(["metrica", "vezes"], ascending=[True, False])


def t5(r):
    planas = r[r["plana"].fillna(False)].drop_duplicates(["fonte", "entidade", "metrica", "agregacao", "fold"])
    tp = planas.groupby("metrica").size().rename("janelas planas").reset_index()
    ep = r[r["plana"].fillna(False)].groupby("metrica")["mae"].median().rename("MAE mediano (p.p.)").reset_index()
    tp = tp.merge(ep, on="metrica", how="left")
    dv = r[r["divergencia"].fillna(False)].groupby(["variante", "modelo"]).size().rename("previsões fora de [0,100]%").reset_index()
    return tp, dv


def t6(r):
    rr = r.assign(classe=r["nivel"].map(NIVEL_CLASSE))
    a = rr[rr["variante"] == "bruto"].groupby(["classe", "modelo"]).agg(
        n=("mase", "size"), MASE=("mase", "median"), vence_naive=("vence_naive", "mean")).reset_index()
    a["vence_naive"] *= 100
    b = rr[rr["modelo"] == "lstm"].groupby(["classe", "variante"]).agg(
        n=("mase", "size"), MASE=("mase", "median"), vence_naive=("vence_naive", "mean")).reset_index()
    b["vence_naive"] *= 100
    return a, b


def t7(r, e):
    """Efeito de borda: viés da estabilizada nos 7 últimos dias do treino, por métrica, e correlação de Spearman
    (série × janela) entre esse viés e o erro de previsão da mesma variante (viés e MAE). O IQR entra como contraste."""
    chave = ["fonte", "entidade", "metrica", "agregacao", "fold"]
    linhas = []
    for var in ("cnn", "baseline"):
        ev = e[e["variante"] == var][chave + ["vies_borda7"]]
        rv = r[r["variante"] == var]
        todos = rv.groupby(chave)[["vies", "mae"]].mean().reset_index().assign(modelo="todos")
        for mod, rm in (("lstm", rv[rv["modelo"] == "lstm"]), ("cnn_lstm", rv[rv["modelo"] == "cnn_lstm"]),
                        ("todos", todos)):
            m = ev.merge(rm[chave + ["vies", "mae"]], on=chave).dropna()
            for met, g in m.groupby("metrica"):
                lin = {"variante": var, "modelo": mod, "metrica": met, "n": len(g),
                       "vies_borda7_mediana": g["vies_borda7"].median(),
                       "vies_borda7_p25": g["vies_borda7"].quantile(.25), "vies_borda7_p75": g["vies_borda7"].quantile(.75),
                       "vies_prev_mediana": g["vies"].median()}
                for alvo in ("vies", "mae"):
                    if len(g) >= 10 and g["vies_borda7"].nunique() > 1:
                        rho, pv = stats.spearmanr(g["vies_borda7"], g[alvo] if alvo == "vies" else g[alvo])
                    else:
                        rho, pv = np.nan, np.nan
                    lin[f"rho_{alvo}"], lin[f"p_{alvo}"] = rho, pv
                linhas.append(lin)
    return pd.DataFrame(linhas)


def t8(mapa):
    """Janelas por série: K teórico (pelo tamanho do histórico) × K efetivo (janelas com 15+ dias observados no
    teste). A diferença vem da lacuna de coleta de julho–agosto/2026."""
    s = ler("SELECT * FROM previsao_wf_serie WHERE execucao_id = %s", (EID,))
    if s.empty:
        return pd.DataFrame(), pd.DataFrame(), s
    s["perdidas"] = s["k_teorico"] - s["k_efetivo"]
    dist = (s.groupby(["k_teorico", "k_efetivo"]).size().rename("séries").reset_index())
    dist["%"] = dist["séries"] / len(s) * 100
    resumo = pd.DataFrame([{
        "séries": len(s), "com janela perdida": int((s["perdidas"] > 0).sum()),
        "% com janela perdida": (s["perdidas"] > 0).mean() * 100, "janelas perdidas (total)": int(s["perdidas"].sum()),
        "K efetivo mediano": s["k_efetivo"].median(), "K efetivo < 2 (sem seleção)": int((s["k_efetivo"] < 2).sum()),
        "K efetivo = 0 (sem resultado)": int((s["k_efetivo"] == 0).sum())}])
    por = s.assign(entidade=s["entidade"].map(mapa).fillna(s["entidade"]))
    return resumo, dist, por


def legiveis(t):
    t = t.copy()
    if "variante" in t:
        t["variante"] = t["variante"].map(VAR).fillna(t["variante"])
    if "modelo" in t:
        t["modelo"] = t["modelo"].map(MOD).fillna(t["modelo"])
    if "metrica" in t:
        t["metrica"] = t["metrica"].map(MET).fillna(t["metrica"])
    return t


# ───────────────────────── figuras ─────────────────────────
def figuras(r, mapa):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cores = {"bruto": "#52514e", "baseline": "#1baf7a", "cnn": "#2a78d6"}
    # r4: MASE mediano por modelo e variante (média diária, todas as métricas)
    g = r[r["agregacao"] == "media"].groupby(["modelo", "variante"])["mase"].median().unstack()
    ordem = [m for m in ("naive", "sazonal7", "linear", "ets", "arima", "lstm", "cnn_lstm") if m in g.index]
    fig, ax = plt.subplots(figsize=(9, 3.4))
    x = np.arange(len(ordem))
    for j, v in enumerate(("bruto", "baseline", "cnn")):
        if v in g:
            ax.bar(x + (j - 1) * 0.27, g.loc[ordem, v], 0.27, color=cores[v], label=VAR[v])
    ax.set_xticks(x, [MOD[m].replace(" (", "\n(") for m in ordem], fontsize=8)
    ax.set_ylabel("MASE mediano")
    ax.axhline(1, color="#898781", lw=0.8, ls="--")
    ax.set_title("Walk-forward: MASE mediano por modelo e estabilização (média diária)", fontsize=10, weight="bold")
    ax.legend(fontsize=8, ncol=3, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(SAIDA / "r4_walk_forward.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    # r2: série bruta (por cima, mais escura) × iqr × cnn, na execução de produção mais recente
    p = ler("""SELECT r.metrica, r.variante, p.dia, p.valor FROM previsao_resultado r
               JOIN previsao_ponto p ON p.resultado_id = r.id
               WHERE r.execucao_id = (SELECT max(id) FROM previsao_execucao WHERE status = 'ok')
                 AND r.fonte = 'vcenter' AND r.entidade = %s AND p.tipo = 'historico'""", (os.environ.get("FIGURA_R2_ENTIDADE", ""),))
    if len(p):
        fig, axs = plt.subplots(1, 3, figsize=(10.5, 3), sharex=True)
        for ax, m in zip(axs, ("cpu", "memoria", "disco")):
            d = p[p["metrica"] == m]
            for v, lw, z in (("cnn", 1.4, 2), ("baseline", 1.2, 2), ("bruto", 1.0, 3)):
                s = d[d["variante"] == v].sort_values("dia")
                ax.plot(pd.to_datetime(s["dia"]), s["valor"], color=cores[v], lw=lw, zorder=z,
                        label=VAR[v] + (" (por cima)" if v == "bruto" else ""))
            ax.set_title({"cpu": "CPU", "memoria": "Memória", "disco": "Disco"}[m], fontsize=9)
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(labelsize=7)
        axs[0].set_ylabel("% de uso")
        axs[0].legend(fontsize=7, frameon=False)
        fig.suptitle(f"{mapa.get(os.environ.get('FIGURA_R2_ENTIDADE', ''), 'Cluster A')}: série bruta e estabilizada (IQR e 1D-CNN)",
                     fontsize=10, weight="bold", y=1.03)
        fig.savefig(SAIDA / "r2_estabilizacao.png", dpi=200, bbox_inches="tight")
        plt.close(fig)


# ───────────────────────── relatório ─────────────────────────
def main():
    SAIDA.mkdir(parents=True, exist_ok=True)
    r, e, falhas, ex = carregar()
    mapa = mapa_anon(pd.concat([r[["fonte", "nivel", "entidade"]], e[["fonte", "nivel", "entidade"]]]))
    sel = selecao(r)
    tabs = {"T1_previsao": t1(r), "T2_estabilizacao": t2(e), "T3_wilcoxon": t3(r)}
    tabs["T4_selecao"], tabs["T4_frequencia"] = t4(sel)
    tabs["T5_planas"], tabs["T5_divergencia"] = t5(r)
    tabs["T6_nivel_modelos"], tabs["T6_nivel_variantes"] = t6(r)
    tabs["T7_borda_cnn"] = t7(r, e)
    tabs["T8_janelas_resumo"], tabs["T8_janelas_dist"], por_serie = t8(mapa)
    if len(por_serie):
        por_serie.to_csv(SAIDA / "T8_janelas_por_serie.csv", index=False, encoding="utf-8")
    for nome, t in tabs.items():
        legiveis(t).to_csv(SAIDA / f"{nome}.csv", index=False, encoding="utf-8")
    if not sel.empty:
        s = sel.assign(entidade=sel["entidade"].map(mapa))
        legiveis(s).to_csv(SAIDA / "T4_selecao_por_serie.csv", index=False, encoding="utf-8")
    figuras(r, mapa)

    series = r.drop_duplicates(["fonte", "entidade", "metrica", "agregacao"])
    x = ex.iloc[0]
    dur = (x["finalizado_em"] - x["iniciado_em"]) if pd.notna(x["finalizado_em"]) else None
    linhas = [
        f"# Resultados do walk-forward (rodada {EID})", "",
        f"- Séries processadas com resultado: **{len(series)}** de {x['series_total']} previstas; "
        f"com erro: **{falhas.drop_duplicates(['fonte', 'entidade', 'metrica', 'agregacao']).shape[0]}**; "
        f"status da rodada: {x['status']}" + (f"; duração: {dur}" if dur is not None else "") + ".",
        f"- Linhas de resultado (série × janela × variante × modelo): {len(r)}; janelas com estabilização medida: {len(e)}.",
        "- Erro sempre contra a série bruta do teste; pré-processamento e modelos ajustados só no treino de cada janela; "
        "previsão recursiva. Variantes: bruta, iqr (IQR 1,5 + interpolação + MinMax) e cnn (IQR + autoencoder 1D-CNN). "
        "Nomes anonimizados.", "",
    ]
    secoes = [
        ("T1. Previsão por métrica × variante × modelo (medianas)", "T1_previsao",
         "MAE e erro do pico em p.p.; vence_naive = % das janelas com MAE menor que o naive sobre a série bruta."),
        ("T2. Estabilização por métrica × variante (medianas, no treino)", "T2_estabilizacao",
         "vies_borda7 = média(estabilizada − bruta) nos 7 últimos dias do treino, de onde a previsão parte."),
        ("T3. Testes de Wilcoxon pareados sobre o MASE (série × janela, sem séries planas)", "T3_wilcoxon",
         "mediana_dif = MASE(a) − MASE(b): negativa favorece o primeiro termo; r_rb = correlação rank-biserial; "
         "p_holm = p corrigido por Holm no conjunto de testes."),
        ("T4. Seleção de modelo por série (desempenho só na última janela)", "T4_selecao", ""),
        ("T4b. Frequência do modelo escolhido", "T4_frequencia", ""),
        ("T5. Séries planas", "T5_planas", ""),
        ("T5b. Previsões fora de [0, 100]% (flag de divergência)", "T5_divergencia", ""),
        ("T6. Por nível — modelos (variante bruta)", "T6_nivel_modelos", ""),
        ("T6b. Por nível — variantes (LSTM+GRU)", "T6_nivel_variantes", ""),
        ("T7. Efeito de borda da estabilização no ponto de partida da previsão", "T7_borda_cnn",
         "vies_borda7 em p.p. (estabilizada − bruta, 7 últimos dias do treino); vies_prev = média(previsão − teste "
         "bruto); rho = Spearman entre vies_borda7 e o viés (rho_vies) ou o MAE (rho_mae) da previsão na mesma "
         "série × janela; 'todos' = média dos 7 modelos na janela. O IQR (baseline) entra como contraste. Não se "
         "criou variante corrigida: o efeito é reportado como característica do método."),
        ("T8. Janelas por série: K teórico × K efetivo (resumo)", "T8_janelas_resumo",
         "K teórico = min(4, (n − 60) // 30); uma janela é descartada quando o teste tem menos de 15 dias observados, "
         "o que acontece nas janelas que caem na lacuna de coleta de julho–agosto/2026. Detalhe por série em "
         "T8_janelas_por_serie.csv."),
        ("T8b. Distribuição do K efetivo", "T8_janelas_dist", ""),
    ]
    t3t = tabs["T3_wilcoxon"]
    poucos = t3t[t3t["n"] < 10] if len(t3t) else t3t
    for titulo, nome, nota in secoes:
        if nome == "T3_wilcoxon":
            nota += (" n = pares série × janela em cada teste. " + (
                f"**Atenção: {len(poucos)} teste(s) com n < 10** (coluna aviso); trate-os como inconclusivos."
                if len(poucos) else "Todos os testes têm n ≥ 10."))
        t = legiveis(tabs[nome])
        linhas += [f"## {titulo}", ""] + ([nota, ""] if nota else [])
        linhas += [md(t, 3 if nome == "T3_wilcoxon" else 2) if len(t) else "_sem dados_", ""]
    linhas += ["## Figuras", "", "![r2](r2_estabilizacao.png)", "", "![r4](r4_walk_forward.png)", "",
               "## Principais achados", "", "_(preenchido após a rodada completa)_", ""]
    (SAIDA / "RESULTADOS_WALKFORWARD.md").write_text("\n".join(linhas), encoding="utf-8")
    print(f"relatório: {SAIDA / 'RESULTADOS_WALKFORWARD.md'}; {len(series)} séries; {len(sel)} seleções")


if __name__ == "__main__":
    main()
