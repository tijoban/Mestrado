# CapacityPlan: coleta, previsão de esgotamento e experimento walk-forward

Código da dissertação de mestrado (PPGTI/IFPB) sobre planejamento de capacidade em *data center*
*on-premise* heterogêneo (VMware vSphere, Nutanix AHV e Kubernetes).

O repositório não contém credenciais, endereços internos nem dados da entidade. Tudo o que é
credencial ou endereço é lido de variáveis de ambiente (ver `.env.example`), e os nomes de clusters,
hosts e clientes saem anonimizados nos resultados da análise.

## Origem do código

| Pasta | Origem (repositório interno) |
|---|---|
| `previsao/` (exceto os 3 arquivos abaixo), `tests/`, `Dockerfile` | motor de previsão, branch `main`, commit `b958b69` |
| `previsao/experimento.py`, `previsao/analise_wf.py`, `previsao/experimento_schema.sql` | branch `experimento-walkforward`, commit `dca6873` (código da rodada 5 do experimento, usada na dissertação) |
| `coleta/` | coletores da aplicação CapacityPlan e do monitoramento (ver tabela abaixo) |
| `mapeamento/` | scripts do mapeamento sistemático (os dados ficam em `../mapeamento/`) |

Os arquivos de `previsao/` citados no apêndice da dissertação mantêm a numeração de linhas original.
Os comandos de previsão e coleta abaixo são executados de dentro desta pasta (`cd scripts`).

### `mapeamento/`
| Arquivo | O que faz |
|---|---|
| `triagem.py` | triagem automática dos registros exportados das bases (critérios de inclusão e exclusão) |
| `checagem_fina.py` | checagem fina dos estudos marcados como INCLUIR ou DÚVIDA |
| `gerar_mapeamento.py` | gera figuras, tabelas, `.bib`, planilha e contagens a partir de `extracao_dados.tsv` |
| `gerar_figuras_defesa.py` | figuras extras da defesa e chaves BibTeX por abordagem |

Como executar: ver `../mapeamento/README.md`.

## Estrutura

### `previsao/`
| Arquivo | O que faz | Lê | Grava |
|---|---|---|---|
| `config.py` | parâmetros (limiar de 80%, janelas, horizonte, épocas), por variável de ambiente | | |
| `db.py` | conexão com o PostgreSQL e retentativa | | |
| `fontes.py` | lê as séries do banco canônico e reamostra em 30 min ou 1 dia | `db_vcenter`, `db_nutanix`, `clientes_vmware` | |
| `estabilizacao.py` | IQR + interpolação + MinMax (baseline) e autoencoder 1D-CNN | | |
| `worker.py` | uma série: LSTM(64)+GRU(32), métricas no *hold-out*, previsão recursiva limitada a [0, 100]%, tendência e data de esgotamento | | |
| `main.py` | job mensal: todas as séries × variantes (bruto, baseline, cnn) | | `previsao_execucao`, `previsao_resultado`, `previsao_ponto`, `previsao_avaliacao` |
| `schema.sql` | tabelas do job mensal e a visão `vw_previsao_esgotamento` | | |
| `experimento.py` | experimento da dissertação: agregação diária (média e P95), *walk-forward* expansivo, pré-processamento só no treino, 7 modelos, métricas contra a série bruta | séries de `fontes.py` | `previsao_wf_execucao`, `previsao_wf_resultado`, `previsao_wf_ponto`, `previsao_wf_estabilizacao`, `previsao_wf_serie` |
| `analise_wf.py` | resume uma rodada: medianas, Wilcoxon pareado com correção de Holm, seleção de modelo por série, tabelas T1–T8 e figuras | `previsao_wf_*` | `previsao_wf_selecao`, arquivos em `SAIDA` |
| `experimento_schema.sql` | tabelas do experimento | | |

`tests/test_trava.py`: testes unitários da trava [0, 100]% (rede sintética e banco substituído por dublê).

### Onde está cada etapa em `previsao/experimento.py`

| Etapa | Função | Linhas |
|---|---|---|
| Separação treino/teste (janelas *walk-forward*) | `janelas()` | 75–86 |
| Pré-processamento (IQR, 1D-CNN), só no treino | `_sem_outliers()`, `_cnn()`, `preprocessar()` | 95–101, 104–130, 133–138 |
| Distorção causada pela estabilização | `metricas_estab()` | 141–154 |
| Previsão: modelos de referência | `prever()` | 158–177 |
| Previsão: LSTM+GRU e CNN-LSTM | `_rede()` | 180–203 |
| Métricas de erro (MAE, RMSE, NRMSE, sMAPE, MASE, R², erro do pico, viés) | `metricas()` | 207–226 |
| Laço de avaliação de uma série | `processar()` | 230–294 |

No job mensal (`previsao/worker.py`): `prever_recursivo()` (32–50), `_modelo()` (66–75), `_metricas()` (78–88),
avaliação no *hold-out* (124–134) e tendência e data de esgotamento (143–169).

### `coleta/`
| Arquivo | Plataforma | Frequência em produção | Grava | Nome original |
|---|---|---|---|---|
| `vcenter_tags.py` | vCenter (tag da categoria *Orgao*) | diária | `tag_vcenter` | `Tags/collect_vcenter.py` |
| `nutanix_tags.py` | Nutanix Prism Central v3 (categoria *centrodecusto*) | diária | `tag_nutanix` | `Tags/collect_nutanix.py` |
| `kubernetes_tags.py` | Kubernetes via Prometheus (label *centrodecusto*) | diária | `tag_k8s` | `Tags/collect_k8s.py` |
| `datacenter_collector.py` | vCenter (pyVmomi) e Nutanix Prism v2: uso real, capacidade resiliente e provisionamento | de hora em hora | `datacenter` | `Zabbix-INFRA/Datacenter/datacenter_collector.py` |
| `storage_collect.py` | NetApp, Pure, vSAN e uso por cliente em cada datastore | diária, 07:00 | `storage_dc`, `storage_cliente_dc` | `api/app/jobs/storage_collect.py` |
| `nutanix_vms.py` | Nutanix Prism Element v1/v2: VMs e CVMs por cluster (roda dentro do `storage_collect`) | diária, 07:00 | `nutanix_vms_dc` | `api/app/jobs/nutanix_vms.py` |
| `db.py` | conexão usada por `storage_collect.py` e `nutanix_vms.py` | | | (adaptado de `api/app/db.py`) |
| `schema_coleta.sql` | DDL da tabela `datacenter` e do resumo diário | | | `Zabbix-INFRA/Datacenter/schema.sql` + `api/schema.sql` |

**Coletores das séries da previsão não incluídos.** As séries lidas pela previsão (`db_vcenter` e
`db_nutanix`, gravadas a cada 30 minutos, e `clientes_vmware`, gravada uma vez por dia) são alimentadas
por coletores que rodam fora deste repositório e cujo código não está aqui. O formato esperado dessas
tabelas está nas consultas de `previsao/fontes.py`.

### `deploy/`
CronJobs de exemplo (Kubernetes) sem nenhum valor embutido: as credenciais vêm de secrets criados fora
do repositório (`envFrom`/`secretRef`), e `<REGISTRY>`/`<PULL_SECRET>` devem ser trocados.

| Arquivo | Agenda |
|---|---|
| `cronjob-previsao.yaml` | dia 1 de cada mês, 02:00 |
| `cronjob-storage-collect.yaml` | diária, 07:00 |
| `cronjob-datacenter-collector.yaml` | de hora em hora |

## Fluxo

```
coleta/*  ─────────────►  banco canônico (PostgreSQL)  ──►  previsao/fontes.py
(e coletores externos:     db_vcenter, db_nutanix,            ├─► estabilizacao.py ► worker.py ► main.py   (job mensal)
 db_vcenter, db_nutanix,   clientes_vmware, datacenter,       │      └─► previsao_execucao / _resultado / _ponto / _avaliacao
 clientes_vmware)          tag_*, storage_*                   └─► experimento.py ► previsao_wf_*  ► analise_wf.py ► tabelas e figuras
```

## Como executar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # preencha
set -a && . ./.env && set +a

python -m unittest tests.test_trava -v                 # testes da trava (não acessa o banco)

FONTES=vcenter NIVEIS=total MAX_SERIES=2 python -m previsao.main   # job mensal, teste rápido
python -m previsao.main                                            # job mensal completo

MAX_SERIES=3 python -m previsao.experimento     # experimento, teste rápido
python -m previsao.experimento                  # rodada completa
EXECUCAO=<id> SAIDA=resultados_wf ANON_MAPA=/caminho/fora/do/repo.csv python -m previsao.analise_wf

python -m coleta.storage_collect                # coletores (um por plataforma)
python -m coleta.datacenter_collector
python -m coleta.vcenter_tags
```

As variáveis necessárias estão em `.env.example`.
