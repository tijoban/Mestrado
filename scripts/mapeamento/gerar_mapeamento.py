"""
Gera os artefatos do mapeamento sistemático a partir da extração de dados.

Entradas (nesta pasta / na pasta acima):
  extracao_dados.tsv      -> uma linha por estudo, campos extraídos na leitura
  ../corpus_final.csv     -> lista final (id, título, ano, base, doi, autores...)
  ../triagem/*.csv        -> números do processo de seleção

Saídas:
  figuras/*.pdf e *.png   -> gráficos (PDF vetorial para o LaTeX)
  tabelas/*.tex           -> tabelas e quadros gerados a partir dos dados
  referencias_mapeamento.bib
  extracao_dados.xlsx     -> planilha de extração formatada

Uso:  python gerar_mapeamento.py
Se corrigir algo em extracao_dados.tsv, basta rodar de novo.
"""

import collections
import csv
import re
import unicodedata
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

AQUI = Path(__file__).resolve().parents[2] / "mapeamento" / "mapeamento_latex"   # dados do mapeamento
NOVO = AQUI.parent
FIG = AQUI / "figuras"
TAB = AQUI / "tabelas"
FIG.mkdir(exist_ok=True)
TAB.mkdir(exist_ok=True)

ORDEM_BASES = ["Scopus", "WoS", "IEEE", "ACM"]
NOME_BASE = {"Scopus": "Scopus", "WoS": "Web of Science", "IEEE": "IEEE Xplore", "ACM": "ACM DL"}

# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
ext = {r["id"]: r for r in csv.DictReader(open(AQUI / "extracao_dados.tsv", encoding="utf-8"), delimiter="\t")}
corpus_all = list(csv.DictReader(open(NOVO / "corpus_final.csv", encoding="utf-8-sig"), delimiter=";"))
sel = {r["doi"].lower(): r for r in csv.DictReader(open(NOVO / "triagem/selecionados_final.csv", encoding="utf-8-sig"), delimiter=";")}

estudos = []
for c in corpus_all:
    if not c["id"]:
        continue
    e = dict(c)
    e.update(ext[c["id"]])
    e["tipo"] = sel.get(c["doi"], {}).get("tipo", "")
    estudos.append(e)
assert len(estudos) == len(ext), "extração e corpus não batem"


def ordem(e):
    b, n = e["id"].split()
    return ORDEM_BASES.index(b), int(n)


estudos.sort(key=ordem)

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def esc(s):
    s = s.replace("\\", r"\textbackslash{}")
    for a, b in [("&", r"\&"), ("%", r"\%"), ("_", r"\_"), ("#", r"\#"), ("$", r"\$"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")]:
        s = s.replace(a, b)
    s = s.replace("R2", "R$^2$").replace("<", r"$<$").replace(">", r"$>$")
    return s


def partes(s):
    return [p.strip() for p in s.split(";") if p.strip() and p.strip() != "n.i."]


def sem_acento(s):
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def contar(func):
    cnt = collections.Counter()
    for e in estudos:
        for k in set(func(e)):
            cnt[k] += 1
    return cnt


# ---------------------------------------------------------------------------
# Categorização (regras explícitas, para o texto poder citar os números)
# ---------------------------------------------------------------------------

def cat_ambiente(e):
    a = e["ambiente"].lower()
    if "on-premise" in a:
        return "Data center on-premise"
    if "privada" in a:
        return "Nuvem privada"
    if "multi data center" in a:
        return "Multi data center"
    if any(k in a for k in ("contêiner", "microsservi", "nativa")):
        return "Contêineres/microsserviços"
    if "gpu" in a:
        return "Cluster GPU (cargas de IA)"
    if re.search(r"bitbrains|materna", e["dataset"].lower()):
        return "Hospedagem corporativa de VMs (Bitbrains/Materna)"
    if "pública" in a:
        return "Nuvem pública (traces de provedores)"
    return "Nuvem/VMs (não especificado)"


def cat_origem_dados(e):
    d = e["dataset"].lower()
    publico = any(k in d for k in ("google", "alibaba", "bitbrains", "materna", "azure", "planetlab", "bytedance",
                                     "erms", "wikipedia", "kaggle", "público", "ecl", "competição"))
    proprio = any(k in d for k in ("próprio", "privado", "produção", "monitoramento", "state grid", "bgp",
                                     "logs", "real de nuvem", "teste de estresse", "microsserviços (qps)"))
    if publico and proprio:
        return "Públicos + próprios"
    if proprio:
        return "Próprios/privados"
    if publico:
        return "Traces públicos"
    return "Não informado"


DATASETS = [
    ("Google Cluster", r"google"), ("Alibaba", r"alibaba"), ("Bitbrains (GWA-T-12)", r"bitbrains"),
    ("Microsoft Azure", r"azure"), ("Materna (GWA-T-13)", r"materna"), ("PlanetLab", r"planetlab"),
    ("ByteDance Cloud", r"bytedance"),
    ("Dados próprios/produção", r"próprio|privado|produção|monitoramento|state grid|bgp|logs|real de nuvem|estresse|qps"),
    ("Outros benchmarks", r"wikipedia|kaggle|ecl|competição|erms|público de workload|sintétic"),
]


def cat_datasets(e):
    d = e["dataset"].lower()
    return [n for n, rx in DATASETS if re.search(rx, d)]


METRICAS = [("MAE", r"\bmae\b"), ("MSE", r"\bmse\b"), ("RMSE", r"\brmse\b"), ("MAPE", r"\bmape\b"),
            ("R²", r"\br2\b"), ("SMAPE", r"smape"), ("NRMSE", r"nrmse"), ("MASE", r"mase"),
            ("Custo computacional", r"latência|tamanho do modelo|tempo de treino"),
            ("Métricas probabilísticas/risco", r"probabil|subestima|top-k")]


def cat_metricas(e):
    m = e["metricas"].lower()
    return [n for n, rx in METRICAS if re.search(rx, m)]


FAMILIAS = [
    ("Recorrentes (LSTM/GRU/RNN)", r"lstm|gru|rnn|recorrente|esrnn"),
    ("Convolucionais (CNN/TCN)", r"cnn|tcn|convolu"),
    ("Transformer/atenção", r"transformer|informer|atenção|attention|loadformer|tft"),
    ("MLP/lineares profundos (N-BEATS, TiDE, TimeMixer)", r"n-beats|n-hits|tide|timemixer|mlp|dlinear"),
    ("Espaço de estados (Mamba/SSM)", r"mamba|ssm"),
    ("Grafos (GCN)", r"gcn|grafo|propagação estrutural"),
    ("Decomposição/domínio da frequência", r"decompos|frequência|wavelet|dct|espectral|tempo-frequência|fan "),
    ("Estatísticos (ARIMA/VAR/Prophet)", r"arima|\bvar\b|prophet|holt|\bar\b|\bma\b"),
    ("ML clássico (SVR/RF/XGBoost)", r"svr|svm|random forest|xgboost"),
    ("Metaheurísticas (GA/PSO/GWO)", r"\bga\b|pso|gwo|grey wolf|lizard|also\b|genétic"),
    ("Ensemble", r"ensemble"),
    ("Transfer learning", r"transfer learning|fine-tuning"),
    ("Generativos/LLM (GAN, Llama)", r"gan\b|llama|llm"),
]


def cat_familias(e):
    t = (e["modelo"] + " " + e["categoria"]).lower()
    return [n for n, rx in FAMILIAS if re.search(rx, t)]


def cat_abordagem(e):
    c = e["categoria"].lower()
    if "comparativo" in c or "benchmark" in c:
        return "Estudo comparativo/benchmark"
    if "ensemble" in c:
        return "Ensemble"
    if "metaheur" in c or "ga + pso" in c or "rede neural + ga" in c:
        return "Híbrido com metaheurística"
    if "estatístico" in c and "híbrido" in c:
        return "Híbrido estatístico + DL"
    if "llm" in c or "generativo" in c:
        return "Generativo/LLM"
    if "híbrido" in c:
        return "Híbrido de deep learning"
    return "Deep learning (arquitetura única)"


def cat_horizonte(e):
    h = e["horizonte"].lower()
    if "um passo e multi" in h:
        return "Um passo e multi-step"
    if "longo" in h:
        return "Longo prazo"
    if "multi" in h or "curto e longo" in h:
        return "Multi-step (curto prazo)"
    if "um passo" in h or "t+1" in h or "1 h" in h:
        return "Um passo"
    return "Não informado"


def cat_validacao(e):
    v = e["validacao"].lower()
    out = []
    if "k-fold" in v:
        out.append("Validação cruzada k-fold")
    if "validação/teste" in v or "treino/validação" in v or "70/15/15" in v or "três" in v:
        out.append("Hold-out treino/validação/teste")
    elif "hold-out" in v:
        out.append("Hold-out simples")
    if "variação" in v or "janela" in v or "retreino" in v or "fluxo" in v or "rolling" in v:
        out.append("Retreino/variação de janela")
    if "semente" in v:
        out.append("Múltiplas sementes")
    if "estatístico" in v:
        out.append("Teste estatístico de significância")
    if "tl" in v or "transfer" in v:
        out.append("Transferência entre domínios")
    if not out:
        out.append("Não informado")
    return out


def cat_maturidade(e):
    m = e["maturidade"].lower()
    if "implantado" in m:
        return "Implantado em produção"
    if "implantação" in m or "estudo de caso" in m:
        return "Implantação experimental/estudo de caso"
    if "protótipo" in m or "prova de conceito" in m:
        return "Protótipo/prova de conceito"
    if "comparativo" in m or "benchmark" in m or "empírico" in m:
        return "Estudo comparativo/benchmark"
    return "Experimento offline"


FERRAMENTAS = [("Python", r"python|pytorch|tensorflow|keras|scikit|pandas"), ("PyTorch", r"pytorch"),
               ("TensorFlow/Keras", r"tensorflow|keras"), ("scikit-learn", r"scikit"), ("Pandas/NumPy", r"pandas|numpy"),
               ("statsmodels", r"statsmodels"), ("Prophet", r"prophet"), ("XGBoost", r"xgboost"),
               ("GPU", r"gpu"), ("Kubernetes", r"kubernetes"), ("Docker", r"docker"), ("Apache Spark", r"spark"),
               ("TensorFlow Serving", r"serving"), ("Streamlit", r"streamlit")]


def cat_ferramentas(e):
    f = e["ferramentas"].lower()
    return [n for n, rx in FERRAMENTAS if re.search(rx, f)]


ALVOS = [("CPU", r"cpu|carga do host|carga de hosts|carga de servidor|workload"), ("Memória", r"memória"),
         ("Disco", r"disco"), ("Rede", r"rede"), ("Requisições/QPS", r"qps|requisições"), ("GPU", r"gpu")]


def cat_alvos(e):
    a = e["alvo"].lower()
    return [n for n, rx in ALVOS if re.search(rx, a)]


# ---------------------------------------------------------------------------
# Estilo dos gráficos
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "font.family": "serif", "font.serif": ["Times New Roman", "DejaVu Serif"], "font.size": 10,
    "axes.spines.top": False, "axes.spines.right": False, "axes.titleweight": "bold",
    "axes.grid": True, "axes.grid.axis": "x", "grid.color": "#dddddd", "grid.linewidth": 0.6,
    "axes.axisbelow": True, "pdf.fonttype": 42, "savefig.bbox": "tight",
})
COR = "#2F5D8A"
CORES_BASES = {"Scopus": "#1B3A5C", "WoS": "#4F7CAC", "IEEE": "#8FB3D9", "ACM": "#C9DDF0"}


def salvar(fig, nome):
    fig.savefig(FIG / f"{nome}.pdf")
    fig.savefig(FIG / f"{nome}.png", dpi=300)
    plt.close(fig)


def barras_h(cnt, nome, xlabel="Número de estudos", total=None, altura=None, cor=COR):
    itens = sorted(cnt.items(), key=lambda kv: (kv[1], kv[0]))
    rot = [k for k, _ in itens]
    val = [v for _, v in itens]
    fig, ax = plt.subplots(figsize=(6.3, altura or max(2.0, 0.34 * len(rot) + 0.6)))
    bars = ax.barh(rot, val, color=cor, height=0.65)
    tot = total or len(estudos)
    for b, v in zip(bars, val):
        ax.text(b.get_width() + max(val) * 0.01, b.get_y() + b.get_height() / 2,
                f"{v} ({100 * v / tot:.0f}%)", va="center", fontsize=8.5)
    ax.set_xlim(0, max(val) * 1.22)
    ax.set_xlabel(xlabel)
    ax.tick_params(axis="y", length=0)
    salvar(fig, nome)


# ---------------------------------------------------------------------------
# Figuras
# ---------------------------------------------------------------------------
N = len(estudos)

# 1. Fluxo de seleção ---------------------------------------------------------
triagem = list(csv.DictReader(open(NOVO / "triagem/triagem_automatica.csv", encoding="utf-8-sig"), delimiter=";"))
checagem = list(csv.DictReader(open(NOVO / "triagem/excluidos_checagem.csv", encoding="utf-8-sig"), delimiter=";"))
rec = collections.Counter(r["fonte"] for r in triagem)
n_rec = len(triagem)
n_dup = sum(1 for r in triagem if r["criterio"] == "E5")
n_unicos = n_rec - n_dup
pre = collections.Counter(r["criterio"] for r in triagem if r["decisao"] == "EXCLUIR" and r["criterio"] != "E5")
n_pre = sum(pre.values())
n_apos_pre = n_unicos - n_pre
chk = collections.Counter(r["criterio_checagem"] for r in checagem)
n_chk = len(checagem)
n_eleg = n_apos_pre - n_chk
n_pagos = sum(1 for c in corpus_all if c["status"].startswith("excluído"))
n_incl = n_eleg - n_pagos
assert n_incl == N, (n_incl, N)

fig, ax = plt.subplots(figsize=(6.6, 8.6))
ax.set_xlim(0, 10)
ax.set_ylim(0, 13.6)
ax.axis("off")
ax.grid(False)


def caixa(x, y, w, h, txt, fc="#F3F6FA", ec=COR, fs=8.6, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12", fc=fc, ec=ec, lw=1))
    ax.text(x + w / 2, y + h / 2, txt, ha="center", va="center", fontsize=fs, linespacing=1.25,
            fontweight="bold" if bold else "normal")


def seta(x1, y1, x2, y2):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle="-|>", color="#444444", lw=1))


etapas = ["Identificação", "Triagem", "Elegibilidade", "Inclusão"]
for et, (y0, y1) in zip(etapas, [(11.0, 13.5), (4.9, 10.8), (2.9, 4.7), (0.1, 2.7)]):
    ax.add_patch(FancyBboxPatch((0.0, y0), 0.55, y1 - y0, boxstyle="round,pad=0.01,rounding_size=0.1", fc=COR, ec=COR))
    ax.text(0.275, (y0 + y1) / 2, et, rotation=90, ha="center", va="center", color="white", fontsize=9, fontweight="bold")

txt_bases = "\n".join(f"{NOME_BASE[b]}: {rec.get(b, 0)}" for b in ORDEM_BASES)
caixa(0.9, 11.2, 4.6, 2.2, f"Registros recuperados nas bases\n(n = {n_rec})\n\n{txt_bases}")
caixa(6.0, 11.7, 3.8, 1.1, f"Duplicados removidos\n(n = {n_dup})", fc="#FFFFFF")
seta(5.5, 12.25, 6.0, 12.25)
seta(3.2, 11.2, 3.2, 10.25)
caixa(0.9, 9.2, 4.6, 1.0, f"Registros únicos triados por título,\nresumo e palavras-chave (n = {n_unicos})")
desc_pre = {"E3": "escalonamento/alocação", "E6": "autoscaling", "E1": "energia/hardware", "E2": "outro alvo de previsão",
            "I1": "fora do contexto de nuvem/DC", "E4": "revisões e outros tipos"}
txt_pre = "\n".join(f"{k} {desc_pre.get(k, k)}: {v}" for k, v in pre.most_common())
caixa(6.0, 8.5, 3.8, 2.6, f"Excluídos na pré-triagem\n(n = {n_pre})\n\n{txt_pre}", fc="#FFFFFF", fs=7.8)
seta(5.5, 9.7, 6.0, 9.7)
seta(3.2, 9.2, 3.2, 7.45)
caixa(0.9, 6.4, 4.6, 1.0, f"Registros na checagem fina\n(previsão como objeto do estudo; n = {n_apos_pre})")
desc_chk = {"F1": "título sem “forecasting”", "I1": "previsão não é o foco", "E2": "outro alvo",
            "E3": "escalonamento/alocação", "manual": "decisão manual", "G1": "previsão parte de sistema",
            "A1": "edge/fog/rede", "A2": "sem ambiente de nuvem", "E7": "SLA/QoS no título",
            "I2": "sem avaliação quantitativa", "L1": "outro idioma"}
txt_chk = "\n".join(f"{desc_chk.get(k, k)}: {v}" for k, v in chk.most_common())
caixa(6.0, 4.5, 3.8, 3.8, f"Excluídos na checagem fina\n(n = {n_chk})\n\n{txt_chk}", fc="#FFFFFF", fs=7.6)
seta(5.5, 6.9, 6.0, 6.9)
seta(3.2, 6.4, 3.2, 4.45)
caixa(0.9, 3.4, 4.6, 1.0, f"Estudos elegíveis para leitura\nna íntegra (n = {n_eleg})")
caixa(6.0, 3.3, 3.8, 1.0, f"Texto completo indisponível\n(acesso pago; n = {n_pagos})", fc="#FFFFFF")
seta(5.5, 3.85, 6.0, 3.85)
seta(3.2, 3.4, 3.2, 2.65)
dist = "\n".join(f"{NOME_BASE[b]} = {sum(1 for e in estudos if e['base'] == b)}" for b in ORDEM_BASES)
caixa(0.9, 0.3, 4.6, 2.3, f"Estudos incluídos no mapeamento\n(n = {n_incl})\n\n{dist}", fc="#DCE7F3")
salvar(fig, "fig_fluxo_selecao")

# 2. Estudos por ano e base ------------------------------------------------
anos = sorted({int(e["ano"]) for e in estudos})
fig, ax = plt.subplots(figsize=(6.3, 3.0))
base_acc = [0] * len(anos)
for b in ORDEM_BASES:
    vals = [sum(1 for e in estudos if e["base"] == b and int(e["ano"]) == a) for a in anos]
    ax.bar([str(a) for a in anos], vals, bottom=base_acc, color=CORES_BASES[b], label=NOME_BASE[b], width=0.62,
           edgecolor="white", linewidth=0.5)
    base_acc = [x + y for x, y in zip(base_acc, vals)]
for i, t in enumerate(base_acc):
    ax.text(i, t + 0.3, str(t), ha="center", fontsize=9)
ax.set_ylabel("Número de estudos")
ax.grid(axis="y")
ax.grid(axis="x", visible=False)
ax.set_ylim(0, max(base_acc) * 1.18)
ax.legend(frameon=False, ncol=4, loc="upper left", fontsize=8.5)
salvar(fig, "fig_estudos_ano_base")

# 3. Tipo de publicação ------------------------------------------------------
# 4. Ambientes, origem dos dados -------------------------------------------
barras_h(contar(lambda e: [cat_ambiente(e)]), "fig_ambientes")
barras_h(contar(lambda e: [cat_origem_dados(e)]), "fig_origem_dados")
barras_h(contar(cat_datasets), "fig_datasets")
barras_h(contar(cat_alvos), "fig_alvos")

# 5. Modelos ------------------------------------------------------------------
barras_h(contar(lambda e: [cat_abordagem(e)]), "fig_abordagens")
barras_h(contar(cat_familias), "fig_familias_modelos")

# Evolução das famílias por ano (mapa de calor)
fams = [f for f, _ in FAMILIAS]
mat = [[sum(1 for e in estudos if int(e["ano"]) == a and f in cat_familias(e)) for a in anos] for f in fams]
fig, ax = plt.subplots(figsize=(6.3, 4.4))
ax.grid(False)
im = ax.imshow(mat, cmap="Blues", aspect="auto", vmin=0)
ax.set_xticks(range(len(anos)), [str(a) for a in anos])
ax.set_yticks(range(len(fams)), fams, fontsize=8.5)
for i in range(len(fams)):
    for j in range(len(anos)):
        v = mat[i][j]
        if v:
            ax.text(j, i, str(v), ha="center", va="center", fontsize=8.5,
                    color="white" if v > max(map(max, mat)) * 0.55 else "#1a1a1a")
ax.spines[:].set_visible(False)
ax.tick_params(length=0)
cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
cb.set_label("Estudos", fontsize=8.5)
cb.outline.set_visible(False)
salvar(fig, "fig_evolucao_familias")

# 6. Métricas, horizonte, validação --------------------------------------------
barras_h(contar(cat_metricas), "fig_metricas")
barras_h(contar(lambda e: [cat_horizonte(e)]), "fig_horizonte")
barras_h(contar(cat_validacao), "fig_validacao")

# 7. Ferramentas e maturidade -------------------------------------------------
barras_h(contar(cat_ferramentas), "fig_ferramentas")
barras_h(contar(lambda e: [cat_maturidade(e)]), "fig_maturidade")

# Baselines mais usados
BASELINES = [("LSTM", r"\blstm\b"), ("GRU", r"\bgru\b"), ("ARIMA/SARIMA", r"arima"), ("Transformer", r"\btransformer\b"),
             ("Informer", r"\binformer\b"), ("Autoformer", r"autoformer"), ("PatchTST", r"patchtst"),
             ("DLinear", r"dlinear"), ("iTransformer", r"itransformer"), ("TimesNet", r"timesnet"),
             ("CNN-LSTM", r"cnn-lstm"), ("BiLSTM", r"bilstm"), ("FEDformer", r"fedformer"),
             ("Persistente/naive", r"persistent|naive"), ("Prophet", r"prophet"), ("XGBoost", r"xgboost")]
cnt_base = contar(lambda e: [n for n, rx in BASELINES if re.search(rx, (e["baselines"] + " " + e["modelo"]).lower())
                             and re.search(rx, e["baselines"].lower())])
barras_h(cnt_base, "fig_baselines")

# ---------------------------------------------------------------------------
# Referências (BibTeX)
# ---------------------------------------------------------------------------

def lista_autores(s):
    return [x.strip() for x in re.split(r";| and ", s) if x.strip()]


def sobrenome(autor):
    a = autor.strip()
    if "," in a:
        return a.split(",")[0].replace(" ", "")
    toks = a.split()
    if not toks:
        return "anonimo"
    if re.fullmatch(r"([A-Z]\.-?)+", toks[0]):  # "S. Vijayakumar"
        return toks[-1]
    while len(toks) > 1 and re.fullmatch(r"([A-Z]\.-?)+", toks[-1]):  # "De Novi D."
        toks = toks[:-1]
    return "".join(toks)


def autores_bib(s):
    out = []
    for a in lista_autores(s):
        if "," in a:
            out.append(a)
            continue
        toks = a.split()
        if re.fullmatch(r"([A-Z]\.-?)+", toks[0]):
            out.append(f"{toks[-1]}, {' '.join(toks[:-1])}")
        else:
            k = len(toks)
            while k > 1 and re.fullmatch(r"([A-Z]\.-?)+", toks[k - 1]):
                k -= 1
            out.append(f"{' '.join(toks[:k])}, {' '.join(toks[k:])}" if k < len(toks) else a)
    return " and ".join(out)


chaves = collections.Counter()
for e in estudos:
    base = re.sub(r"[^a-z]", "", sem_acento(sobrenome(lista_autores(e["autores"])[0])).lower()) + e["ano"]
    chaves[base] += 1
    e["chave"] = base if chaves[base] == 1 else base + "abcdefg"[chaves[base] - 1]
# garante que a primeira de uma colisão também fique única
dups = {k for k, v in chaves.items() if v > 1}
for e in estudos:
    if e["chave"] in dups:
        e["chave"] += "a"

with open(AQUI / "referencias_mapeamento.bib", "w", encoding="utf-8") as f:
    for e in estudos:
        artigo = e["tipo"] == "Article" or "transactions" in e["veiculo"].lower() or "journal" in e["veiculo"].lower()
        campo = "journal" if artigo else "booktitle"
        f.write(f"@{'article' if artigo else 'inproceedings'}{{{e['chave']},\n"
                f"  author    = {{{autores_bib(e['autores'])}}},\n"
                f"  title     = {{{{{e['titulo']}}}}},\n"
                f"  {campo:<9} = {{{e['veiculo']}}},\n"
                f"  year      = {{{e['ano']}}},\n"
                f"  doi       = {{{e['doi']}}},\n"
                f"  note      = {{ID no mapeamento: {e['id']}}}\n}}\n\n")

# ---------------------------------------------------------------------------
# Tabelas / quadros (LaTeX)
# ---------------------------------------------------------------------------

def escreve(nome, txt):
    (TAB / nome).write_text(txt, encoding="utf-8")


# Tabela: distribuição por base
sel59 = collections.Counter(c["base"] for c in corpus_all)
inc = collections.Counter(e["base"] for e in estudos)
linhas = []
for b in ORDEM_BASES:
    pct = f"{100 * inc[b] / N:.1f}".replace(".", ",")
    linhas.append(f"{NOME_BASE[b]} & {rec.get(b, 0)} & {sel59[b]} & {inc[b]} & {pct}\\% \\\\")
escreve("tab_distribuicao_bases.tex", rf"""\begin{{table}}[htbp]
\centering
\caption{{Distribuição dos registros e dos estudos incluídos por base de dados}}
\label{{tab:map-bases}}
\begin{{tabular}}{{lrrrr}}
\toprule
\textbf{{Base}} & \textbf{{Recuperados}} & \textbf{{Elegíveis}} & \textbf{{Incluídos}} & \textbf{{\% do total}} \\
\midrule
{chr(10).join(linhas)}
\midrule
\textbf{{Total}} & \textbf{{{n_rec}}} & \textbf{{{len(corpus_all)}}} & \textbf{{{N}}} & \textbf{{100,0\%}} \\
\bottomrule
\end{{tabular}}
\par\medskip\footnotesize
Nota: estudos indexados em mais de uma base foram atribuídos à base com menos registros. Elegíveis: após a checagem fina; Incluídos: com texto completo disponível.\\
Fonte: Elaborado pelo autor (2026).
\end{{table}}
""")


def longtable(nome, legenda, rotulo, colspec, cab, linhas, fonte="Fonte: Elaborado pelo autor (2026).", tipo="quadro"):
    ncol = len(re.findall(r"p\{[^}]*\}|[lcr]", colspec))
    if tipo == "quadro":
        legenda_tex = ""
        antes = f"\\mapquadro{{{legenda}}}{{{rotulo}}}\n"
    else:
        legenda_tex = f"\\caption{{{legenda}}}\\label{{{rotulo}}}\\\\\n"
        antes = ""
    txt = antes + rf"""{{\footnotesize
\setlength{{\tabcolsep}}{{3pt}}
\renewcommand{{\arraystretch}}{{1.15}}
\begin{{longtable}}{{{colspec}}}
{legenda_tex}\toprule
{cab} \\
\midrule
\endfirsthead
\multicolumn{{{ncol}}}{{l}}{{\textit{{Continuação}}}}\\
\toprule
{cab} \\
\midrule
\endhead
\midrule
\multicolumn{{{ncol}}}{{r}}{{\textit{{Continua na próxima página}}}}\\
\endfoot
\bottomrule
\multicolumn{{{ncol}}}{{l}}{{{fonte}}}\\
\endlastfoot
{chr(10).join(linhas)}
\end{{longtable}}
}}
"""
    escreve(nome, txt)


# Quadro: catálogo dos estudos
linhas = []
for e in estudos:
    tipo = "Periódico" if (e["tipo"] == "Article" or "Transactions" in e["veiculo"]) else "Evento"
    outras = [b for b in partes(e["bases"]) if b != e["base"]]
    linhas.append(f"{e['id']} & {esc(e['titulo'])} \\cite{{{e['chave']}}} & {e['ano']} & {tipo} & "
                  f"{esc(', '.join(outras)) if outras else '--'} \\\\")
longtable("qua_catalogo_estudos.tex", "Estudos primários incluídos no mapeamento", "qua:map-catalogo",
          r"p{1.4cm}p{9.3cm}p{0.9cm}p{1.5cm}p{1.7cm}",
          r"\textbf{ID} & \textbf{Título e referência} & \textbf{Ano} & \textbf{Veículo} & \textbf{Também em}",
          linhas)

# Quadro: ambiente e dados (QP2/QP3)
linhas = [f"{e['id']} & {esc(e['ambiente'])} & {esc(e['dataset'])} & {esc(e['alvo'])} & {esc(e['granularidade'])} \\\\"
          for e in estudos]
longtable("qua_ambiente_dados.tex", "Ambiente, dados e alvo de previsão dos estudos (QP2 e QP3)", "qua:map-ambiente",
          r"p{1.3cm}p{3.2cm}p{5.0cm}p{3.5cm}p{1.8cm}",
          r"\textbf{ID} & \textbf{Ambiente} & \textbf{Conjunto de dados} & \textbf{Alvo} & \textbf{Granul.}",
          linhas)

# Quadro: modelos (QP4)
linhas = [f"{e['id']} & {esc(cat_abordagem(e))} & {esc(e['modelo'])} & {esc(e['achado'])} \\\\" for e in estudos]
longtable("qua_modelos.tex", "Abordagem, modelo proposto e principal resultado de cada estudo (QP4)", "qua:map-modelos",
          r"p{1.3cm}p{2.6cm}p{5.0cm}p{5.9cm}",
          r"\textbf{ID} & \textbf{Abordagem} & \textbf{Modelo proposto/avaliado} & \textbf{Principal resultado}",
          linhas)

# Tabela: matriz de métricas e protocolo (QP5)
cols = ["MAE", "MSE", "RMSE", "MAPE", "R²"]
linhas = []
for e in estudos:
    m = cat_metricas(e)
    outras = [x for x in m if x not in cols]
    marca = " & ".join(r"$\bullet$" if c in m else "" for c in cols)
    linhas.append(f"{e['id']} & {marca} & {esc(', '.join(outras)) or '--'} & {esc(e['validacao'])} & {esc(cat_horizonte(e))} \\\\")
tot = " & ".join(str(sum(1 for e in estudos if c in cat_metricas(e))) for c in cols)
linhas.append(r"\midrule" + f"\n\\textbf{{Total}} & {tot} & & & \\\\")
longtable("tab_metricas_validacao.tex", "Métricas de avaliação, protocolo de validação e horizonte por estudo (QP5)",
          "tab:map-metricas", r"p{1.3cm}ccccc p{2.0cm}p{3.2cm}p{2.0cm}",
          r"\textbf{ID} & \textbf{MAE} & \textbf{MSE} & \textbf{RMSE} & \textbf{MAPE} & \textbf{R$^2$} & \textbf{Outras} & "
          r"\textbf{Validação} & \textbf{Horizonte}", linhas, tipo="tabela")

# Quadro: ferramentas e maturidade (QP3/QP6)
linhas = [f"{e['id']} & {esc(e['ferramentas'])} & {esc(e['maturidade'])} \\\\" for e in estudos]
longtable("qua_ferramentas.tex", "Linguagens, bibliotecas, infraestrutura e maturidade das soluções (QP3 e QP6)",
          "qua:map-ferramentas", r"p{1.3cm}p{8.8cm}p{4.9cm}",
          r"\textbf{ID} & \textbf{Ferramentas e infraestrutura relatadas} & \textbf{Maturidade}", linhas)

# Tabela-resumo de frequências (para citar no texto)
resumo = {
    "Ambiente": contar(lambda e: [cat_ambiente(e)]),
    "Origem dos dados": contar(lambda e: [cat_origem_dados(e)]),
    "Abordagem": contar(lambda e: [cat_abordagem(e)]),
    "Horizonte": contar(lambda e: [cat_horizonte(e)]),
    "Maturidade": contar(lambda e: [cat_maturidade(e)]),
}
linhas = []
for dim, cnt in resumo.items():
    itens = cnt.most_common()
    for i, (k, v) in enumerate(itens):
        rot = f"\\multirow{{{len(itens)}}}{{*}}{{\\textbf{{{dim}}}}}" if i == 0 else ""
        linhas.append(f"{rot} & {esc(k)} & {v} & {100 * v / N:.1f}\\% \\\\".replace(".", ","))
    linhas.append(r"\midrule")
linhas = linhas[:-1]
escreve("tab_sintese_categorias.tex", rf"""\begin{{table}}[htbp]
\centering
\caption{{Síntese da classificação dos {N} estudos por dimensão analisada}}
\label{{tab:map-sintese}}
\footnotesize
\begin{{tabular}}{{llrr}}
\toprule
\textbf{{Dimensão}} & \textbf{{Categoria}} & \textbf{{Estudos}} & \textbf{{\%}} \\
\midrule
{chr(10).join(linhas)}
\bottomrule
\end{{tabular}}
\par\medskip
Fonte: Elaborado pelo autor (2026).
\end{{table}}
""")

# ---------------------------------------------------------------------------
# Planilha de extração
# ---------------------------------------------------------------------------
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

wb = Workbook()
ws = wb.active
ws.title = "Extração"
campos = [("id", "ID"), ("chave", "Chave BibTeX"), ("titulo", "Título"), ("ano", "Ano"), ("base", "Base"),
          ("bases", "Bases onde aparece"), ("veiculo", "Veículo"), ("doi", "DOI"), ("ambiente", "Ambiente (QP2)"),
          ("plataforma", "Plataforma"), ("dataset", "Dataset (QP3)"), ("alvo", "Alvo de previsão"),
          ("categoria", "Categoria do modelo (QP4)"), ("modelo", "Modelo proposto/avaliado"), ("baselines", "Baselines"),
          ("metricas", "Métricas (QP5)"), ("validacao", "Validação"), ("horizonte", "Horizonte"),
          ("granularidade", "Granularidade"), ("ferramentas", "Ferramentas (QP3)"), ("maturidade", "Maturidade (QP6)"),
          ("achado", "Principal resultado")]
ws.append([c[1] for c in campos] + ["Cat. ambiente", "Origem dos dados", "Abordagem", "Horizonte (cat.)"])
for e in estudos:
    ws.append([e[c[0]] for c in campos] + [cat_ambiente(e), cat_origem_dados(e), cat_abordagem(e), cat_horizonte(e)])
for c in ws[1]:
    c.font = Font(bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="2F5D8A")
    c.alignment = Alignment(wrap_text=True, vertical="center")
larg = {"Título": 50, "Modelo proposto/avaliado": 40, "Principal resultado": 50, "Dataset (QP3)": 30, "Baselines": 30}
for i, (_, rot) in enumerate(campos + [("", "Cat. ambiente"), ("", "Origem dos dados"), ("", "Abordagem"), ("", "Horizonte (cat.)")], 1):
    ws.column_dimensions[get_column_letter(i)].width = larg.get(rot, 18)
for row in ws.iter_rows(min_row=2):
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
ws.freeze_panes = "C2"
ws.auto_filter.ref = ws.dimensions

ws2 = wb.create_sheet("Excluídos (pagos)")
ws2.append(["Base", "Título", "Ano", "DOI", "Situação"])
for c in corpus_all:
    if not c["id"]:
        ws2.append([c["base"], c["titulo"], c["ano"], c["doi"], c["status"]])
ws2.column_dimensions["B"].width = 80
ws2.column_dimensions["D"].width = 35

ws3 = wb.create_sheet("Frequências")
for dim, func in [("Ambiente", lambda e: [cat_ambiente(e)]), ("Origem dos dados", lambda e: [cat_origem_dados(e)]),
                  ("Datasets", cat_datasets), ("Alvo", cat_alvos), ("Abordagem", lambda e: [cat_abordagem(e)]),
                  ("Família de modelo", cat_familias), ("Métricas", cat_metricas), ("Validação", cat_validacao),
                  ("Horizonte", lambda e: [cat_horizonte(e)]), ("Ferramentas", cat_ferramentas),
                  ("Maturidade", lambda e: [cat_maturidade(e)]), ("Baselines", None)]:
    cnt = cnt_base if func is None else contar(func)
    ws3.append([dim, "Estudos", "%"])
    ws3[ws3.max_row][0].font = Font(bold=True)
    for k, v in cnt.most_common():
        ws3.append([k, v, round(100 * v / N, 1)])
    ws3.append([])
ws3.column_dimensions["A"].width = 50
wb.save(AQUI / "extracao_dados.xlsx")

# ---------------------------------------------------------------------------
# Números para o texto
# ---------------------------------------------------------------------------
with open(AQUI / "numeros_para_texto.txt", "w", encoding="utf-8") as f:
    f.write(f"Recuperados: {n_rec} {dict(rec)} | duplicados {n_dup} | únicos {n_unicos}\n")
    f.write(f"Excluídos pré-triagem: {n_pre} {dict(pre)}\nApós pré-triagem: {n_apos_pre}\n")
    f.write(f"Excluídos checagem fina: {n_chk} {dict(chk)}\nElegíveis: {n_eleg} | pagos: {n_pagos} | incluídos: {N}\n")
    f.write(f"Por ano: {dict(collections.Counter(e['ano'] for e in estudos))}\n")
    f.write(f"Tipo: {dict(collections.Counter(e['tipo'] for e in estudos))}\n\n")
    for dim, func in [("Ambiente", lambda e: [cat_ambiente(e)]), ("Origem", lambda e: [cat_origem_dados(e)]),
                      ("Datasets", cat_datasets), ("Alvo", cat_alvos), ("Abordagem", lambda e: [cat_abordagem(e)]),
                      ("Famílias", cat_familias), ("Métricas", cat_metricas), ("Validação", cat_validacao),
                      ("Horizonte", lambda e: [cat_horizonte(e)]), ("Ferramentas", cat_ferramentas),
                      ("Maturidade", lambda e: [cat_maturidade(e)])]:
        f.write(f"{dim}: {contar(func).most_common()}\n")
    f.write(f"Baselines: {cnt_base.most_common()}\n\n")
    for dim, func in [("Ambiente", lambda e: [cat_ambiente(e)]), ("Abordagem", lambda e: [cat_abordagem(e)]),
                      ("Origem", lambda e: [cat_origem_dados(e)]), ("Maturidade", lambda e: [cat_maturidade(e)]),
                      ("Horizonte", lambda e: [cat_horizonte(e)])]:
        grupos = collections.defaultdict(list)
        for e in estudos:
            for k in func(e):
                grupos[k].append(e["chave"])
        for k, v in grupos.items():
            f.write(f"[{dim}] {k}: {','.join(v)}\n")
    f.write("\nChaves: " + "; ".join(f"{e['id']}={e['chave']}" for e in estudos) + "\n")

print(f"OK: {N} estudos; figuras em {FIG.name}/, tabelas em {TAB.name}/")
