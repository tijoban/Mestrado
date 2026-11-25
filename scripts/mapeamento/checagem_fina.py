"""
Segunda checagem (mais rigorosa) sobre o resultado de triagem.py.

Parte de triagem/triagem_automatica.csv, considera só INCLUIR e DUVIDA e
mantém apenas os artigos cujo objeto principal é PREVER uso/demanda de
recursos computacionais:

  - o título precisa anunciar a previsão de recurso/carga
    (ex.: "workload prediction", "CPU utilization forecasting");
  - o título não pode tratar de escalonamento, alocação, migração,
    offloading, balanceamento etc. (E3);
  - o título não pode ter outro alvo/foco: custo, falha, anomalia,
    energia, latência, desempenho... (E1/E2);
  - SLA/QoS/SLO no título exclui (E7);
  - o resumo precisa indicar avaliação quantitativa (I2).

Depois das regras, aplica as decisões manuais (REVISAO_MANUAL), feitas
lendo título e resumo, para corrigir casos que as regras erram.

Refinamento final (para chegar a ~60 estudos):
  - F1: o título precisa usar "forecasting" (previsão de série temporal);
    títulos só com "prediction" saem;
  - A1: ambiente fora de nuvem/data center (edge, MEC, fog, VNF, rede,
    servidores de jogos) ou foco em aprendizado federado/privacidade;
  - G1: previsão como parte de sistema de gestão/provisionamento/otimização
    (o objeto do estudo é o sistema, não o modelo de previsão);
  - A2: título sem nuvem/data center/VM/contêiner/cluster/servidor/CPU/memória;
  - L1: artigo em outro idioma (título em chinês).

Duplicados: cada artigo é atribuído à base com MENOS registros entre as
bases onde aparece (sai da base que contém mais).

Saídas (pasta triagem/):
  selecionados_final.csv  -> artigos que ficaram, com a base e as bases onde também aparecem
  selecionados_final.md   -> a mesma lista, agrupada por base, para leitura
  excluidos_checagem.csv  -> os que saíram nesta etapa e o motivo

Uso:
  python checagem_fina.py
"""

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from triagem import ALVO_PREVISAO, AVALIACAO, ENERGIA, OUTRA_METRICA, OUTRO_ALVO, PRIORIDADE_FONTE, titulo_norm

PASTA = Path(__file__).resolve().parents[2] / "mapeamento" / "triagem"
csv.field_size_limit(10**9)

SLA = re.compile(r"\bsla\b|\bslas\b|\bqos\b|\bslos?\b|service[- ]level|quality of service", re.I)
ESCALONAMENTO = re.compile(
    r"schedul|allocat|placement|consolidat|migrat|offload|load[- ]balanc|orchestrat|overcommit|co-?locat"
    r"|brokerage|admission|replica|deploy|right-?siz|configuration|management framework|caching|partition",
    re.I)
OUTRO_FOCO = re.compile(
    r"\bcost|financ|finops|price|pricing|billing|maintenance|anomal|failure|fault|aging|security|attack"
    r"|privacy|trajector|vehic|mobility|telematic|carbon|green|sustainab|energy|power|temperature|thermal"
    r"|latency|runtime|execution time|response time|performance|throughput|stress|reliab|availability"
    r"|5g|6g|\bran\b|wireless|iot\b|offloading|task type|interference|query",
    re.I)

FORA_NUVEM = re.compile(r"\bedge\b|\bmecs?\b|\bfog\b|compute continuum|network service|\bvnf|game server"
                        r"|federated|gossip|privacy", re.I)
GESTAO = re.compile(r"management|provision|kubernetes|aiops|distribution|resilient|database-as-a-service"
                    r"|optimizing|forecasting service", re.I)
AMBIENTE_TITULO = re.compile(r"cloud|data ?cent|\bvms?\b|virtual machine|container|cluster|server|serverless"
                             r"|iaas|microservice|\bcpu\b|memory", re.I)


def refinar(r):
    """Refinamento final sobre os que passaram em checar(). Retorna (fica, criterio, motivo)."""
    titulo = r["titulo"]
    if not re.search(r"forecast", titulo, re.I):
        return False, "F1", "título usa prediction, não forecasting"
    m = FORA_NUVEM.search(titulo)
    if m:
        return False, "A1", f"fora de nuvem/data center ou foco federado: {m.group(0).lower()}"
    m = GESTAO.search(titulo)
    if m:
        return False, "G1", f"previsão como parte de sistema de {m.group(0).lower()}"
    if not AMBIENTE_TITULO.search(titulo):
        return False, "A2", "título sem nuvem/data center/VM/contêiner/servidor/CPU/memória"
    if re.search(r"[一-鿿]", titulo):
        return False, "L1", "artigo em chinês"
    return True, r["criterio_checagem"], r["motivo_checagem"]


# Decisões tomadas lendo título + resumo, indexadas pelo TÍTULO (os ids mudam a cada exportação).
REVISAO_MANUAL = {
    "A robust ensemble technique in forecasting workload of local healthcare departments":
        ("EXCLUIR", "I1 manual: carga de trabalho de departamentos de saúde, não de TI"),
    "An Integrated Predictive Analytics Architecture for Coordinated Sales Demand and Customer Lifetime Value Forecasting":
        ("EXCLUIR", "I1 manual: previsão de demanda de vendas"),
    "An Adaptive Blockchain Framework for Federated IoMT with Reinforcement Learning-Based Consensus and Resource Forecasting":
        ("EXCLUIR", "I1 manual: blockchain/IoMT; previsão é acessória"),
    "Cognitive Load Estimation Using a Hybrid Cluster-Based Unsupervised Machine Learning Technique":
        ("EXCLUIR", "I1 manual: carga cognitiva humana"),
    "Research on Multi-Model Fusion Machine Learning Demand Intelligent Forecasting System in Cloud Computing Environment":
        ("EXCLUIR", "I1 manual: resumo da base não corresponde ao título (geração de testes com LLM)"),
    "A Hybrid Deep Learning-Based Load Forecasting Model for Logical Range":
        ("EXCLUIR", "I1 manual: carga de dispositivos em Logical Range (simulação), fora de nuvem/data center"),
    "Revealing DRAM Operating GuardBands through Workload-Aware Error Predictive Modeling":
        ("EXCLUIR", "E1 manual: erros de DRAM (hardware)"),
    "Very-Short-Term Load Forecasting for AI-Data Centers Using Gaussian Process Regression":
        ("EXCLUIR", "E1 manual: carga elétrica de data center de IA"),
    "Periodicity-Aware AI Data Center IT Load Forecasting Across Heterogeneous Cluster Scenarios":
        ("EXCLUIR", "E1 manual: carga elétrica (IT load) para despacho na rede elétrica"),
    "Data Center Computing Load Forecasting Based on Feature Modeling and the Informer Model":
        ("EXCLUIR", "E1 manual: previsão de consumo de energia a partir da carga computacional"),
    "A Bandwidth Prediction Method Based on Hybrid LSTM for Content Delivery Network":
        ("EXCLUIR", "E2 manual: banda de rede de CDN (tráfego), não recurso de data center"),
    "Beyond One-Step: Attention-Based Encoder–Decoder for Multi-Step Forecasts for Network Load Dataset":
        ("EXCLUIR", "E2 manual: carga de rede de telecomunicações"),
    "An Improved LightGBM Job Running Status Prediction Algorithm Integrating Combinatorial Feature Selection and Bayesian Hyperparameter Optimization on Spark":
        ("EXCLUIR", "E2 manual: prevê status de execução de jobs"),
    "A Hierarchical Deep Learning Approach for Predicting Job Queue Times in HPC Systems":
        ("EXCLUIR", "E2 manual: prevê tempo de fila de jobs"),
    "Log-Transformed Regime-Based Prediction of Cloud Job Length Using Machine Learning":
        ("EXCLUIR", "E2 manual: prevê duração de jobs"),
    "Predicting Job Turnaround Time in Large-Scale Distributed Computing Environments with Graph Neural Networks":
        ("EXCLUIR", "E2 manual: prevê turnaround time de jobs"),
    "Adaptive Serverless Resource Management via Slot-Survival Prediction and Event-Driven Lifecycle Control":
        ("EXCLUIR", "E2 manual: prevê sobrevivência de slots serverless"),
    "Emerging VM Threat Prediction and Dynamic Workload Estimation for Secure Resource Management in Industrial Clouds":
        ("EXCLUIR", "E2 manual: foco em ameaças/segurança de VMs"),
    "Effective low capacity status prediction for cloud systems":
        ("EXCLUIR", "E2 manual: prevê falhas de alocação (low capacity status)"),
    "From Feature Selection to Resource Prediction: An Analysis of Commonly AppliedWorkflows and Techniques":
        ("EXCLUIR", "E2 manual: prevê desempenho de workloads de banco de dados"),
    "Coded Distributed Computing With Predictive Heterogeneous User Demands: A Learning Auction Approach":
        ("EXCLUIR", "E3 manual: leilão/alocação em computação distribuída codificada"),
    "Deep Learning Approach for Workload Prediction and Balancing in Cloud Computing":
        ("EXCLUIR", "E3 manual: previsão acoplada a balanceamento de carga"),
    "Enhancing Cloud Resource Management through Predictive Neural Networks and Reinforcement Learning":
        ("EXCLUIR", "E6 manual: escalonamento de microsserviços com RL (autoscaling)"),
    "A Deep Learning-Based Workload Forecasting Model in Cloud Data Centers":
        ("EXCLUIR", "E2 manual: foco em ataques adversariais a modelos CNN-LSTM, com dados de sensores IoT"),
    "Forecasting CPU and Memory Usage in Cloud Workloads Using Task Metadata: A Machine Learning Approach on Google Cluster Data":
        ("EXCLUIR", "F1 manual: regressão do uso médio a partir de metadados de tarefas, não previsão de série temporal"),
}
REVISAO_MANUAL = {titulo_norm(t): v for t, v in REVISAO_MANUAL.items()}


def carregar():
    with open(PASTA / "triagem_automatica.csv", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def checar(r):
    """Retorna (fica: bool, criterio, motivo)."""
    titulo, resumo = r["titulo"], r["resumo"]

    if titulo_norm(titulo) in REVISAO_MANUAL:
        decisao, motivo = REVISAO_MANUAL[titulo_norm(titulo)]
        return decisao == "INCLUIR", "manual", motivo

    alvo = [m.group(0) for m in ALVO_PREVISAO.finditer(titulo)]
    if not alvo:
        return False, "I1", "título não anuncia previsão de uso/demanda de recursos"
    # Só no título: no resumo, SLA costuma ser só a motivação de artigos de previsão.
    if SLA.search(titulo):
        return False, "E7", "título foca SLA/QoS/SLO"
    m = ESCALONAMENTO.search(titulo)
    if m:
        return False, "E3", f"título trata de {m.group(0).lower()}"
    m = OUTRO_FOCO.search(titulo) or OUTRO_ALVO.search(titulo) or OUTRA_METRICA.search(titulo) \
        or ENERGIA.search(titulo)
    if m:
        return False, "E2", f"título tem outro foco: {m.group(0).lower()}"
    if resumo and not AVALIACAO.search(resumo):
        return False, "I2", "resumo sem indício de avaliação quantitativa"
    return True, "I", "previsão de recursos é o objeto do estudo: " + ", ".join(a.lower() for a in alvo[:2])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    todos = carregar()

    # Bases onde cada artigo aparece (a mantida + as cópias marcadas como duplicado).
    bases = defaultdict(set)
    for r in todos:
        bases[r["duplicado_de"] or r["id"]].add(r["fonte"])

    # Duplicado fica na base com menos registros (sai da que contém mais).
    tamanho_base = Counter(r["fonte"] for r in todos)

    candidatos = [r for r in todos if r["decisao"] != "EXCLUIR"]
    ficaram, sairam = [], []
    for r in candidatos:
        fica, criterio, motivo = checar(r)
        r["criterio_checagem"], r["motivo_checagem"] = criterio, motivo
        if fica:
            fica, r["criterio_checagem"], r["motivo_checagem"] = refinar(r)
        r["bases"] = ", ".join(sorted(bases[r["id"]], key=tamanho_base.get))
        r["fonte"] = min(bases[r["id"]], key=tamanho_base.get)
        (ficaram if fica else sairam).append(r)

    ficaram.sort(key=lambda r: (PRIORIDADE_FONTE[r["fonte"]], r["ano"], r["titulo"].lower()))
    colunas = ["id", "titulo", "ano", "fonte", "bases", "tipo", "veiculo", "autores", "doi", "motivo_checagem"]
    with open(PASTA / "selecionados_final.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colunas, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(ficaram)
    with open(PASTA / "excluidos_checagem.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "criterio_checagem", "motivo_checagem", "titulo", "ano",
                                          "fonte", "bases", "doi"], delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(sairam, key=lambda r: (r["criterio_checagem"], r["id"])))

    linhas = [f"# Artigos selecionados ({len(ficaram)})", ""]
    for fonte in sorted(PRIORIDADE_FONTE, key=PRIORIDADE_FONTE.get):
        grupo = [r for r in ficaram if r["fonte"] == fonte]
        if not grupo:
            continue
        linhas += [f"## {fonte} ({len(grupo)})", ""]
        for n, r in enumerate(grupo, 1):
            outras = r["bases"].replace(fonte, "").strip(", ")
            extra = f" — também em: {outras}" if outras else ""
            doi = f" — doi:{r['doi']}" if r["doi"] else ""
            linhas.append(f"{n}. {r['titulo']} ({r['ano']}){doi}{extra}")
        linhas.append("")
    (PASTA / "selecionados_final.md").write_text("\n".join(linhas), encoding="utf-8")

    print(f"Candidatos (INCLUIR + DUVIDA da triagem): {len(candidatos)}")
    print(f"Ficaram: {len(ficaram)}   Saíram: {len(sairam)}")
    print("\nSaíram por critério:")
    for c, n in sorted(Counter(r["criterio_checagem"] for r in sairam).items()):
        print(f"  {c:7} {n}")
    print("\nFicaram por base (base mantida após deduplicação):")
    for fonte, n in sorted(Counter(r["fonte"] for r in ficaram).items(), key=lambda x: PRIORIDADE_FONTE[x[0]]):
        print(f"  {fonte:9} {n}")
    print("\nFicaram por base (contando todas as bases em que o artigo aparece):")
    for fonte in sorted(PRIORIDADE_FONTE, key=PRIORIDADE_FONTE.get):
        print(f"  {fonte:9} {sum(fonte in r['bases'] for r in ficaram)}")
    print(f"  TOTAL     {len(ficaram)} artigos únicos")
    print(f"\nLista: {PASTA / 'selecionados_final.md'}")


if __name__ == "__main__":
    main()
