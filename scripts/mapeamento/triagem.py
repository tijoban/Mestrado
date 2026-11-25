"""
Pré-triagem automática dos registros exportados das bases (Scopus, IEEE Xplore,
ACM DL e Springer Link) para o mapeamento sistemático sobre avaliação de modelos
de previsão de uso de recursos computacionais.

O script:
  1. lê os quatro exports e unifica os campos;
  2. remove duplicados (DOI ou título normalizado) -> E5;
  3. aplica os critérios I1-I3 / E1-E5 por regras de palavras-chave sobre
     título + resumo + palavras-chave;
  4. grava a planilha com a decisão (INCLUIR / DUVIDA / EXCLUIR), o critério e
     os termos que motivaram a decisão;
  5. gera lotes de 40 registros (INCLUIR + DUVIDA) no formato do PROMPT 1.

Modo rigoroso (padrão): só INCLUI quando o título anuncia previsão de uso/demanda
de recursos em nuvem/data center/cluster/VM/contêiner e o resumo indica avaliação
quantitativa. DUVIDA fica restrita a casos de fronteira; o resto é excluído.
Período: 2021 em diante.
É uma PRÉ-triagem: as decisões devem ser conferidas (prompt 1 / leitura humana).

Uso:
  python triagem.py
  python triagem.py --controle controle.txt   # um DOI ou título por linha
"""

import argparse
import csv
import html
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

PASTA = Path(__file__).resolve().parents[2] / "mapeamento"   # exports das bases (não publicados); ou use --pasta
ANO_MIN, ANO_MAX = 2021, 2027  # 2027 = artigos "online first" já com volume do ano seguinte
TAMANHO_LOTE = 40

csv.field_size_limit(10**9)


# ---------------------------------------------------------------------------
# Leitura das bases
# ---------------------------------------------------------------------------

def limpar(txt):
    txt = html.unescape(txt or "")
    txt = re.sub(r"[{}\\]", "", txt)
    return re.sub(r"\s+", " ", txt).strip()


def ler_csv(caminho):
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def ler_scopus(caminho):
    for r in ler_csv(caminho):
        yield {
            "fonte": "Scopus",
            "titulo": r["Title"], "ano": r["Year"], "resumo": r["Abstract"],
            "palavras_chave": "; ".join(x for x in (r["Author Keywords"], r["Index Keywords"]) if x),
            "doi": r["DOI"], "veiculo": r["Source title"], "tipo": r["Document Type"],
            "autores": r["Authors"],
        }


def ler_ieee(caminho):
    for r in ler_csv(caminho):
        yield {
            "fonte": "IEEE",
            "titulo": r["Document Title"], "ano": r["Publication Year"], "resumo": r["Abstract"],
            "palavras_chave": "; ".join(x for x in (r["Author Keywords"], r["IEEE Terms"]) if x),
            "doi": r["DOI"], "veiculo": r["Publication Title"],
            "tipo": "Conference paper" if re.search(r"conference|symposium|workshop", r["Publication Title"], re.I) else "Article",
            "autores": r["Authors"],
        }


def ler_wos(caminho):
    # Export do Web of Science (.xls) salvo como CSV; o Excel em pt-BR usa ";" como separador.
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        delim = ";" if f.readline().count(";") > f.readline().count(",") else ","
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        linhas = list(csv.DictReader(f, delimiter=delim))
    for r in linhas:
        yield {
            "fonte": "WoS",
            "titulo": r["Article Title"], "ano": r["Publication Year"], "resumo": r["Abstract"],
            "palavras_chave": "; ".join(x for x in (r["Author Keywords"], r["Keywords Plus"]) if x),
            "doi": r["DOI"], "veiculo": r["Source Title"],
            "tipo": "Conference paper" if "Proceedings" in r["Document Type"] else
                    "Review" if "Review" in r["Document Type"] else "Article",
            "autores": r["Authors"],
        }


def ler_springer(caminho):
    # O export do Springer Link não traz resumo nem palavras-chave.
    for r in ler_csv(caminho):
        yield {
            "fonte": "Springer",
            "titulo": r["Item Title"], "ano": r["Publication Year"], "resumo": "",
            "palavras_chave": "", "doi": r["Item DOI"], "veiculo": r["Publication Title"],
            "tipo": r["Content Type"], "autores": r["Authors"],
        }


def _valor_bib(texto, i):
    """Lê um valor BibTeX a partir de texto[i] ({...}, "..." ou palavra)."""
    if texto[i] == "{":
        nivel, j = 0, i
        while j < len(texto):
            if texto[j] == "{":
                nivel += 1
            elif texto[j] == "}":
                nivel -= 1
                if nivel == 0:
                    return texto[i + 1:j], j + 1
            j += 1
        return texto[i + 1:], len(texto)
    if texto[i] == '"':
        j = texto.index('"', i + 1)
        return texto[i + 1:j], j + 1
    m = re.match(r"[^,}\s]+", texto[i:])
    return m.group(0), i + m.end()


def ler_bib(caminho):
    texto = Path(caminho).read_text(encoding="utf-8")
    for m in re.finditer(r"@(\w+)\s*\{([^,]*),", texto):
        tipo_bib = m.group(1).lower()
        i, campos = m.end(), {}
        while True:
            c = re.compile(r"\s*(\w+)\s*=\s*").match(texto, i)
            if not c:
                break
            valor, i = _valor_bib(texto, c.end())
            campos[c.group(1).lower()] = valor
            i = re.compile(r"\s*,?").match(texto, i).end()
        tipos = {"article": "Article", "inproceedings": "Conference paper", "inbook": "Book chapter",
                 "proceedings": "Proceedings", "book": "Book"}
        yield {
            "fonte": "ACM",
            "titulo": campos.get("title", ""), "ano": campos.get("year", ""),
            "resumo": campos.get("abstract", ""), "palavras_chave": campos.get("keywords", ""),
            "doi": campos.get("doi", ""), "veiculo": campos.get("booktitle") or campos.get("journal", ""),
            "tipo": tipos.get(tipo_bib, tipo_bib), "autores": campos.get("author", ""),
        }


def carregar_tudo(pasta):
    leitores = []
    for arq in sorted(pasta.iterdir()):
        if arq.suffix.lower() == ".bib":
            leitores.append((arq.name, ler_bib(arq)))
        elif arq.suffix.lower() == ".csv" and not arq.name.startswith("triagem"):
            cab = arq.open(encoding="utf-8-sig").readline()
            if '"EID"' in cab or "Author full names" in cab:
                leitores.append((arq.name, ler_scopus(arq)))
            elif "Document Title" in cab:
                leitores.append((arq.name, ler_ieee(arq)))
            elif "Item Title" in cab:
                leitores.append((arq.name, ler_springer(arq)))
            elif "Article Title" in cab and "UT (Unique WOS ID)" in cab:
                leitores.append((arq.name, ler_wos(arq)))
            else:
                print(f"[aviso] formato não reconhecido, ignorado: {arq.name}", file=sys.stderr)
    registros = []
    for nome, leitor in leitores:
        for r in leitor:
            r = {k: limpar(v) for k, v in r.items()}
            r["doi"] = r["doi"].lower().removeprefix("https://doi.org/")
            r["arquivo"] = nome
            registros.append(r)
    return registros


# ---------------------------------------------------------------------------
# Duplicados (E5)
# ---------------------------------------------------------------------------

def titulo_norm(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


# Ao escolher qual cópia fica como original: a com resumo e a da base mais completa.
PRIORIDADE_FONTE = {"Scopus": 0, "WoS": 1, "IEEE": 2, "ACM": 3, "Springer": 4}


def marcar_duplicados(registros):
    registros.sort(key=lambda r: (not r["resumo"], PRIORIDADE_FONTE[r["fonte"]]))
    contador = Counter()
    vistos = {}
    for r in registros:
        prefixo = r["fonte"][:3].upper()
        contador[prefixo] += 1
        r["id"] = f"{prefixo}{contador[prefixo]:04d}"
        chaves = [k for k in (("doi", r["doi"]), ("tit", titulo_norm(r["titulo"]))) if k[1]]
        original = next((vistos[k] for k in chaves if k in vistos), None)
        r["duplicado_de"] = original or ""
        for k in chaves:
            vistos.setdefault(k, original or r["id"])
    return registros


# ---------------------------------------------------------------------------
# Critérios
# ---------------------------------------------------------------------------

def rx(*termos):
    return re.compile(r"\b(?:" + "|".join(termos) + r")", re.I)


PREVISAO = rx(r"predict", r"forecast", r"prognos", r"time[- ]series", r"lstm", r"\bgru\b", r"arima",
              r"transformer", r"neural network", r"deep learning", r"machine learning")
VERBO_PREVISAO = rx(r"predict", r"forecast", r"anticipat", r"estimat")

AMBIENTE = rx(r"cloud", r"data ?cent(?:er|re)s?", r"cluster", r"virtual machine", r"\bvms?\b", r"container",
              r"kubernetes", r"docker", r"microservice", r"serverless", r"edge computing", r"fog",
              r"\bhpc\b", r"high[- ]performance comput", r"supercomput", r"\bservers?\b", r"iaas", r"\bpaas",
              r"web (?:application|server|service)", r"hosts?\b")
AMBIENTE_FORTE = rx(r"cloud", r"data ?cent(?:er|re)s?", r"cluster", r"virtual machine", r"\bvms?\b",
                    r"container", r"kubernetes", r"microservice", r"serverless", r"\bhpc\b", r"supercomput",
                    r"iaas", r"\bpaas")

RECURSO = (r"workloads?", r"cpu", r"processor", r"memory", r"\bram\b", r"disk", r"storage", r"i/o",
           r"resource(?:s)?(?: usage| utili[sz]ation| demand| consumption| requirement| provisioning)?",
           r"(?:host|server|machine|vm|container) ?load", r"load", r"utili[sz]ation", r"usage", r"demand",
           r"requests?", r"jobs?", r"gpu", r"bandwidth", r"capacity")
RECURSO_RX = rx(*RECURSO)

# "workload prediction", "CPU usage forecasting", "predict(ing) the future workload" ...
_rec = "(?:" + "|".join(RECURSO) + ")"
ALVO_PREVISAO = re.compile(
    r"\b" + _rec + r"[\w\s\-/]{0,45}?\b(?:predict|forecast|prognos|estimat)"
    r"|\b(?:predict|forecast)\w*\s+(?:\w+\s+){0,5}?" + _rec, re.I)

AVALIACAO = rx(r"rmse", r"\bmae\b", r"mape", r"smape", r"mase", r"\bmse\b", r"r2\b", r"r-squared",
               r"accura", r"error", r"outperform", r"compar", r"baseline", r"benchmark", r"experiment",
               r"evaluat", r"dataset", r"trace", r"real[- ]world", r"improve\w* by", r"\d+(?:\.\d+)?\s?%")

# E1: energia / refrigeração / hardware
ENERGIA = rx(r"energy", r"power consumption", r"power usage", r"cooling", r"thermal", r"temperature",
             r"carbon", r"\bpue\b", r"electricity", r"battery", r"hardware design", r"chip", r"fpga",
             r"power (?:forecast|predict|efficien|demand|management)")
# E6: autoscaling / escalonamento elástico (fora do escopo, por decisão do protocolo)
AUTOSCALING = rx(r"auto[- ]?scal", r"scal(?:ing|er)s?\b", r"\bhpa\b", r"elastic (?:scaling|provisioning)")
# E2: previsão de outra coisa
OUTRO_ALVO = rx(r"failure", r"fault", r"anomal", r"intrusion", r"attack", r"malware", r"\bprice", r"pricing",
                r"spot instance price", r"cellular", r"mobile traffic", r"5g", r"6g", r"base station",
                r"vehicular", r"\biot traffic", r"smart grid", r"electric(?:al|ity) load", r"solar\b", r"wind\b",
                r"stock\b", r"disk failure", r"remaining useful life", r"churn", r"qos prediction",
                r"web service qos", r"latency prediction", r"congestion control", r"\bssd\b")
PREVISAO_TITULO = rx(r"predict", r"forecast", r"proactive", r"anticipat")
# Outros alvos de previsão (desempenho, não uso de recursos)
OUTRA_METRICA = rx(r"runtime", r"run time", r"execution time", r"response time", r"latency", r"\bqos\b",
                   r"duration", r"completion time", r"throughput", r"performance prediction", r"\bsla\b")
# Previsão de recursos declarada como contribuição na mesma frase do resumo
CONTRIBUICAO = re.compile(r"(?:propos|present|develop|introduc|design)\w*\b[^.]{0,120}?(?:" +
                          ALVO_PREVISAO.pattern + r")", re.I)
# E3: escalonamento / alocação sem previsão avaliada
ESCALONAMENTO = rx(r"schedul", r"allocat", r"placement", r"consolidat", r"migration", r"auto-?scal",
                   r"offload", r"load balanc", r"optimi[sz]")
# E4: revisões etc.
REVISAO_TITULO = rx(r"survey", r"review", r"systematic", r"tutorial", r"state[- ]of[- ]the[- ]art",
                    r"taxonomy", r"overview", r"literature", r"editorial", r"challenges and",
                    r"recent (?:trends|advances|developments)", r"comprehensive study of")
TIPOS_EXCLUIDOS = {"Review", "Short survey", "Conference review", "Erratum", "Letter", "Book", "Retracted",
                   "Proceedings", "Editorial", "Note"}


def termos(regex, texto, n=4):
    achados = []
    for m in regex.finditer(texto):
        t = m.group(0).strip().lower()
        if t not in achados:
            achados.append(t)
    return achados[:n]


def classificar(r):
    """Retorna (decisao, criterio, justificativa, pontuacao).

    Modo rigoroso: INCLUIR só quando o TÍTULO anuncia a previsão de uso/demanda
    de recursos, o contexto é nuvem/data center/cluster/VM/contêiner e o resumo
    indica avaliação quantitativa. DUVIDA fica restrita a casos em que a
    previsão de recursos é a contribuição declarada no resumo, mas não no
    título, ou o título mistura previsão com escalonamento/outro alvo.
    Todo o resto é EXCLUIR.
    """
    if r["duplicado_de"]:
        return "EXCLUIR", "E5", f"duplicado de {r['duplicado_de']}", 0

    try:
        ano = int(r["ano"][:4])
    except ValueError:
        ano = None
    if ano is None or not ANO_MIN <= ano <= ANO_MAX:
        return "EXCLUIR", "I3", f"ano {r['ano'] or '?'} fora de {ANO_MIN}-{ANO_MAX}", 0
    if r["tipo"] in TIPOS_EXCLUIDOS:
        return "EXCLUIR", "E4", f"tipo de publicação: {r['tipo']}", 0

    titulo, resumo = r["titulo"], r["resumo"]
    texto = " ".join((titulo, resumo, r["palavras_chave"]))
    so_titulo = not resumo

    if REVISAO_TITULO.search(titulo):
        return "EXCLUIR", "E4", "título indica revisão/survey: " + ", ".join(termos(REVISAO_TITULO, titulo)), 0
    if AUTOSCALING.search(titulo):
        return "EXCLUIR", "E6", "título trata de autoscaling: " + ", ".join(termos(AUTOSCALING, titulo)), 0

    # "job failure prediction", "power load forecasting" não contam como alvo de recurso.
    alvo_tit = [m.group(0).strip().lower() for m in ALVO_PREVISAO.finditer(titulo)
                if not re.search(r"failure|fault|power|energy", m.group(0), re.I)
                and not OUTRA_METRICA.search(m.group(0))
                and not re.search(r"(?:power|energy|thermal|electric\w*|grid)[\s-]*$", titulo[:m.start()], re.I)]
    alvo_tit = list(dict.fromkeys(alvo_tit))[:4]
    if not alvo_tit and ALVO_PREVISAO.search(titulo):
        return "EXCLUIR", "E2", "título prevê outro alvo: " + ALVO_PREVISAO.search(titulo).group(0), 0
    alvo_res = termos(ALVO_PREVISAO, resumo + " " + r["palavras_chave"])
    n_alvo_res = len(ALVO_PREVISAO.findall(resumo))
    amb_forte = termos(AMBIENTE_FORTE, texto)
    aval = termos(AVALIACAO, resumo)
    fora_tit = termos(OUTRO_ALVO, titulo) + termos(OUTRA_METRICA, titulo)
    energia_tit = termos(ENERGIA, titulo)
    # "optimized by PSO" no título costuma ser ajuste de hiperparâmetro, não escalonamento.
    esc_tit = [t for t in termos(ESCALONAMENTO, titulo) if not t.startswith("optimi")]
    contribuicao = CONTRIBUICAO.search(resumo)

    pontos = (3 * bool(alvo_tit) + 2 * bool(alvo_res) + bool(amb_forte) + bool(aval)
              + bool(contribuicao) - bool(fora_tit) - bool(energia_tit) - bool(esc_tit))

    if not amb_forte:
        if so_titulo and alvo_tit:
            return "DUVIDA", "D", f"só título; alvo {', '.join(alvo_tit)}, ambiente não citado", pontos
        if alvo_tit and not fora_tit and re.search(r"\bedge\b|\bfog\b", texto, re.I):
            return "DUVIDA", "D", f"título: {', '.join(alvo_tit)}; ambiente edge/fog", pontos
        return "EXCLUIR", "I1", "sem contexto de nuvem/data center/cluster/VM/contêiner", pontos

    if energia_tit and not alvo_tit:
        return "EXCLUIR", "E1", "título foca energia/hardware: " + ", ".join(energia_tit), pontos
    if fora_tit and not alvo_tit:
        return "EXCLUIR", "E2", "título prevê outro alvo: " + ", ".join(fora_tit), pontos

    if alvo_tit:
        if fora_tit or energia_tit:
            return "DUVIDA", "D", f"título: {', '.join(alvo_tit)}, mas também {', '.join(fora_tit + energia_tit)}", pontos
        if esc_tit:
            return "DUVIDA", "D", f"título une previsão ({', '.join(alvo_tit)}) e {', '.join(esc_tit)}", pontos
        if so_titulo:
            return "INCLUIR", "I", f"só título (sem resumo); alvo: {', '.join(alvo_tit)}", pontos
        if not aval:
            return "DUVIDA", "D", "título ok, resumo sem indício de avaliação quantitativa", pontos
        return "INCLUIR", "I", f"título: {', '.join(alvo_tit)}; avaliação: {', '.join(aval[:3])}", pontos

    # Título não anuncia previsão de recursos.
    titulo_no_tema = AMBIENTE_FORTE.search(titulo) or RECURSO_RX.search(titulo) \
        or re.search(r"time[- ]series", titulo, re.I)
    if PREVISAO_TITULO.search(titulo) and not fora_tit and not energia_tit \
            and (n_alvo_res >= 2 or (titulo_no_tema and (n_alvo_res or re.search(r"forecast|time[- ]series", titulo, re.I)))):
        return "DUVIDA", "D", f"título preditivo ({PREVISAO_TITULO.search(titulo).group(0)}); resumo: " \
            + ", ".join(alvo_res), pontos
    if contribuicao and n_alvo_res >= 2 and not esc_tit and not fora_tit and not energia_tit \
            and (RECURSO_RX.search(titulo) or AMBIENTE_FORTE.search(titulo)):
        return "DUVIDA", "D", "previsão de recursos declarada como contribuição só no resumo: " \
            + contribuicao.group(0)[:80], pontos
    if esc_tit or ESCALONAMENTO.search(texto):
        return "EXCLUIR", "E3", "previsão, se houver, é componente de " \
            + ", ".join(esc_tit or termos(ESCALONAMENTO, texto)), pontos
    if termos(ENERGIA, texto) and not alvo_res:
        return "EXCLUIR", "E1", "foco em energia: " + ", ".join(termos(ENERGIA, texto)), pontos
    if termos(OUTRO_ALVO, texto) and not alvo_res:
        return "EXCLUIR", "E2", "prevê outro alvo: " + ", ".join(termos(OUTRO_ALVO, texto)), pontos
    return "EXCLUIR", "I1", "previsão de uso de recursos não é o foco do estudo", pontos


# ---------------------------------------------------------------------------
# Saídas
# ---------------------------------------------------------------------------

COLUNAS = ["id", "decisao", "criterio", "justificativa", "pontuacao", "fonte", "arquivo", "ano", "tipo", "titulo",
           "autores", "veiculo", "doi", "duplicado_de", "palavras_chave", "resumo"]


def gravar_planilha(registros, caminho):
    ordem = {"INCLUIR": 0, "DUVIDA": 1, "EXCLUIR": 2}
    registros = sorted(registros, key=lambda r: (ordem[r["decisao"]], -r["pontuacao"], r["id"]))
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(registros)


def gravar_lotes(registros, pasta):
    pasta.mkdir(exist_ok=True)
    for antigo in pasta.glob("lote_*.txt"):
        antigo.unlink()
    selecionados = [r for r in registros if r["decisao"] in ("INCLUIR", "DUVIDA")]
    selecionados.sort(key=lambda r: r["id"])
    for n, i in enumerate(range(0, len(selecionados), TAMANHO_LOTE), start=1):
        linhas = [" | ".join((r["id"], r["ano"], r["titulo"], r["resumo"] or "(sem resumo)",
                              r["palavras_chave"] or "-", r["fonte"]))
                  for r in selecionados[i:i + TAMANHO_LOTE]]
        (pasta / f"lote_{n:03d}.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return len(selecionados)


def verificar_controle(registros, caminho):
    print(f"\nConjunto de controle ({caminho.name}):")
    linhas = [l.strip() for l in caminho.read_text(encoding="utf-8").splitlines() if l.strip()]
    por_doi = {r["doi"]: r for r in registros if r["doi"]}
    por_tit = {titulo_norm(r["titulo"]): r for r in registros}
    achados = 0
    for l in linhas:
        r = por_doi.get(l.lower().removeprefix("https://doi.org/")) or por_tit.get(titulo_norm(l))
        if r and r["duplicado_de"]:
            r = next(x for x in registros if x["id"] == r["duplicado_de"])
        if r:
            achados += 1
            print(f"  [ok] {r['id']} {r['decisao']:8} {l[:80]}")
        else:
            print(f"  [--] não recuperado: {l[:80]}")
    print(f"  recuperados: {achados}/{len(linhas)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pasta", type=Path, default=PASTA, help="pasta com os exports (.csv/.bib)")
    ap.add_argument("--saida", type=Path, default=None, help="pasta de saída (padrão: <pasta>/triagem)")
    ap.add_argument("--controle", type=Path, help="arquivo com DOIs/títulos que a busca deveria recuperar")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    saida =args.saida or args.pasta / "triagem"
    saida.mkdir(exist_ok=True)

    registros = marcar_duplicados(carregar_tudo(args.pasta))
    for r in registros:
        r["decisao"], r["criterio"], r["justificativa"], r["pontuacao"] = classificar(r)

    gravar_planilha(registros, saida / "triagem_automatica.csv")
    n_lotes = gravar_lotes(registros, saida / "lotes")

    print(f"Registros lidos: {len(registros)}")
    for fonte, n in Counter(r["fonte"] for r in registros).most_common():
        print(f"  {fonte:9} {n}")
    print("\nDecisões:")
    for d in ("INCLUIR", "DUVIDA", "EXCLUIR"):
        print(f"  {d:8} {sum(r['decisao'] == d for r in registros)}")
    print("\nPor critério:")
    for c, n in sorted(Counter(r["criterio"] for r in registros).items()):
        print(f"  {c:3} {n}")
    print("\nIncluídos + dúvida por fonte (após deduplicação):")
    for fonte, n in Counter(r["fonte"] for r in registros if r["decisao"] != "EXCLUIR").most_common():
        print(f"  {fonte:9} {n}")
    print(f"\nPlanilha: {saida / 'triagem_automatica.csv'}")
    print(f"Lotes para o PROMPT 1: {saida / 'lotes'} ({n_lotes} registros, {TAMANHO_LOTE} por lote)")

    if args.controle:
        verificar_controle(registros, args.controle)

    resumo_final(registros)


def resumo_final(registros):
    fontes = sorted(PRIORIDADE_FONTE, key=PRIORIDADE_FONTE.get)
    colunas = [
        ("Lidos", lambda r: True),
        ("Únicos", lambda r: not r["duplicado_de"]),
        ("Incluir", lambda r: r["decisao"] == "INCLUIR"),
        ("Dúvida", lambda r: r["decisao"] == "DUVIDA"),
        ("Selecionados", lambda r: r["decisao"] != "EXCLUIR"),
    ]
    print("\n" + "=" * 70)
    print("TOTAL DE ARTIGOS POR BASE")
    print("=" * 70)
    print(f"{'Base':10}" + "".join(f"{nome:>12}" for nome, _ in colunas))
    for fonte in fontes + ["TOTAL"]:
        sub = [r for r in registros if fonte == "TOTAL" or r["fonte"] == fonte]
        if fonte == "TOTAL":
            print("-" * 70)
        print(f"{fonte:10}" + "".join(f"{sum(map(f, sub)):>12}" for _, f in colunas))
    print("(Selecionados = Incluir + Dúvida, já sem duplicados; seguem para a triagem do PROMPT 1)")
    print("\nPor arquivo (para conferir com o total mostrado pela base):")
    for arq, n in sorted(Counter(r["arquivo"] for r in registros).items()):
        sel = sum(r["arquivo"] == arq and r["decisao"] != "EXCLUIR" for r in registros)
        print(f"  {arq:40} {n:>6} lidos  {sel:>5} selecionados")


if __name__ == "__main__":
    main()
