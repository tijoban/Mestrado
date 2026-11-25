# Mestrado (PPGTI/IFPB): planejamento de capacidade em data center on-premise

Material da dissertação sobre planejamento de capacidade e previsão de esgotamento em *data center*
*on-premise* heterogêneo (VMware vSphere, Nutanix AHV e Kubernetes).

| Onde | Fase | O que tem |
|---|---|---|
| `mapeamento/` | defesa | mapeamento sistemático da literatura: corpus de 53 estudos, triagem, extração de dados, figuras, tabelas e texto LaTeX |
| `scripts/` | defesa | todo o código: coleta, previsão de esgotamento, experimento *walk-forward*, análise estatística, CronJobs e os scripts do mapeamento |
| raiz (`*.py`) | qualificação | scripts da qualificação, mantidos no lugar porque o texto da qualificação aponta para eles |

Os scripts da raiz foram substituídos, na defesa, pelo código de `scripts/` (ver `scripts/README.md`).

Nenhum arquivo contém credenciais ou endereços internos: o código lê tudo de variáveis de ambiente
(`scripts/.env.example`).
