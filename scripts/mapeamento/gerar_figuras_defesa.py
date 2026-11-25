"""
Figuras extras do mapeamento para a versão da defesa (23/11/2026).

Reaproveita a leitura e as regras de categorização de gerar_mapeamento.py
(executa só a parte de leitura, categorias e chaves BibTeX) e grava em
../../mapeamento/mapeamento_latex/figuras:

  fig_bases.pdf          -> registros recuperados, elegíveis e incluídos por base (antiga tabela)
  fig_qp1_coleta.pdf     -> QP1: paradigma, origem/coleta e armazenamento descritos
  fig_ambientes3.pdf     -> QP2: ambiente em três classes (nuvem, on-premise, híbrido)
  chaves_por_abordagem.txt -> chaves BibTeX dos 53 estudos agrupadas por abordagem
                              (usado em Trabalhos Relacionados)

Uso:  python gerar_figuras_defesa.py
"""
import collections
from pathlib import Path

AQUI = Path(__file__).resolve().parent
DADOS = AQUI.parents[1] / "mapeamento" / "mapeamento_latex"   # dados do mapeamento no repositório
SRC = (AQUI / "gerar_mapeamento.py").read_text(encoding="utf-8")
corte = SRC.index("with open(AQUI / \"referencias_mapeamento.bib\"")
ns = {"__file__": str(AQUI / "gerar_mapeamento.py"), "__name__": "gerar_base"}
exec(compile(SRC[:corte], "gerar_mapeamento.py", "exec"), ns)

plt = ns["plt"]
estudos = ns["estudos"]
N = ns["N"]
COR = ns["COR"]
SAIDA = DADOS / "figuras"
SAIDA.mkdir(parents=True, exist_ok=True)


def salvar(fig, nome):
    fig.savefig(SAIDA / f"{nome}.pdf")
    fig.savefig(SAIDA / f"{nome}.png", dpi=300)
    plt.close(fig)


def rotulo(v, tot):
    return f"{v} ({100 * v / tot:.1f}%)".replace(".", ",")


# 1. Bases (substitui a tabela de distribuição) -------------------------------
rec, corpus_all = ns["rec"], ns["corpus_all"]
ORD, NOME = ns["ORDEM_BASES"], ns["NOME_BASE"]
eleg = collections.Counter(c["base"] for c in corpus_all)
inc = collections.Counter(e["base"] for e in estudos)
fig, axs = plt.subplots(1, 2, figsize=(6.6, 2.6), gridspec_kw={"width_ratios": [1.15, 1]})
y = range(len(ORD))
ax = axs[0]
ax.barh([NOME[b] for b in ORD][::-1], [rec.get(b, 0) for b in ORD][::-1], color="#C9DDF0", height=0.65)
for i, b in enumerate(ORD[::-1]):
    ax.text(rec.get(b, 0) + 8, i, str(rec.get(b, 0)), va="center", fontsize=8.5)
ax.set_xlim(0, max(rec.values()) * 1.22)
ax.set_title("Registros recuperados", fontsize=9.5)
ax.tick_params(axis="y", length=0)
ax = axs[1]
ax.barh([NOME[b] for b in ORD][::-1], [eleg[b] for b in ORD][::-1], color="#8FB3D9", height=0.65, label="Elegíveis")
ax.barh([NOME[b] for b in ORD][::-1], [inc[b] for b in ORD][::-1], color=COR, height=0.65, label="Incluídos")
for i, b in enumerate(ORD[::-1]):
    ax.text(eleg[b] + 0.4, i, rotulo(inc[b], N), va="center", fontsize=8.5)
ax.set_xlim(0, max(eleg.values()) * 1.6)
ax.set_yticklabels([])
ax.tick_params(axis="y", length=0)
ax.set_title(f"Elegíveis e incluídos (n = {N})", fontsize=9.5)
ax.legend(frameon=False, fontsize=8, loc="lower right")
salvar(fig, "fig_bases")

# 2. QP1: paradigma, coleta e armazenamento -----------------------------------
origem = ns["contar"](lambda e: [ns["cat_origem_dados"](e)])
prontos = origem.get("Traces públicos", 0)
proprios = origem.get("Próprios/privados", 0) + origem.get("Públicos + próprios", 0)
ni = origem.get("Não informado", 0)
armazenamento = 1  # só lin2025c descreve o armazenamento (banco de séries temporais)
grupos = [
    ("Paradigma", [("Orientado a dados", N), ("Modelo canônico", 0), ("Ontologia", 0)]),
    ("Origem e coleta", [("Traces públicos prontos", prontos), ("Coleta de dados próprios", proprios),
                         ("Não informado", ni)]),
    ("Armazenamento", [("Descrito", armazenamento), ("Não descrito", N - armazenamento)]),
]
fig, axs = plt.subplots(len(grupos), 1, figsize=(6.3, 3.9), gridspec_kw={"height_ratios": [3, 3, 2]})
for ax, (tit, itens) in zip(axs, grupos):
    rot = [k for k, _ in itens][::-1]
    val = [v for _, v in itens][::-1]
    ax.barh(rot, val, color=COR, height=0.6)
    for i, v in enumerate(val):
        ax.text(v + 0.6, i, rotulo(v, N), va="center", fontsize=8.5)
    ax.set_xlim(0, N * 1.25)
    ax.set_title(tit, fontsize=9.5, loc="left")
    ax.tick_params(axis="y", length=0)
axs[-1].set_xlabel("Número de estudos")
fig.tight_layout()
salvar(fig, "fig_qp1_coleta")

# 3. QP2: três classes de ambiente --------------------------------------------
def classe(e):
    c = ns["cat_ambiente"](e)
    if c in ("Data center on-premise", "Nuvem privada"):
        return "On-premise (data center próprio ou nuvem privada)"
    return "Nuvem (pública, hospedagem, contêineres, GPU, n.i.)"

cnt = collections.Counter(classe(e) for e in estudos)
itens = [("Nuvem (pública, hospedagem, contêineres, GPU, n.i.)", cnt.get("Nuvem (pública, hospedagem, contêineres, GPU, n.i.)", 0)),
         ("On-premise (data center próprio ou nuvem privada)", cnt.get("On-premise (data center próprio ou nuvem privada)", 0)),
         ("Híbrido", 0)]
fig, ax = plt.subplots(figsize=(6.3, 1.8))
rot = [k for k, _ in itens][::-1]
val = [v for _, v in itens][::-1]
ax.barh(rot, val, color=COR, height=0.6)
for i, v in enumerate(val):
    ax.text(v + 0.6, i, rotulo(v, N), va="center", fontsize=8.5)
ax.set_xlim(0, N * 1.25)
ax.set_xlabel("Número de estudos")
ax.tick_params(axis="y", length=0)
salvar(fig, "fig_ambientes3")
onprem = sorted(e["id"] + " " + e["chave"] for e in estudos if classe(e).startswith("On-premise"))

# 4. Chaves por abordagem (para citar todos os estudos) -----------------------
por = collections.defaultdict(list)
for e in estudos:
    por[ns["cat_abordagem"](e)].append(e["chave"])
with open(DADOS / "chaves_por_abordagem.txt", "w", encoding="utf-8") as f:
    for k, v in sorted(por.items(), key=lambda kv: -len(kv[1])):
        f.write(f"{k} ({len(v)}): {','.join(v)}\n")
    f.write(f"\nOn-premise: {onprem}\n")
    f.write(f"Classes: {dict(cnt)}\nOrigem: {dict(origem)}\nBases: rec={dict(rec)} eleg={dict(eleg)} inc={dict(inc)}\n")
print(open(DADOS / "chaves_por_abordagem.txt", encoding="utf-8").read())
