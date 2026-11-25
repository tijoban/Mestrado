# Mapeamento sistemático da literatura

Mapeamento sobre previsão de uso de recursos e planejamento de capacidade em *data centers*
(Scopus, Web of Science, IEEE Xplore e ACM Digital Library, 2021–2026). O corpus final tem **53 estudos**.

| Arquivo/pasta | O que é |
|---|---|
| `corpus_final.csv` | lista final: ID (ex.: "IEEE 18"), base, título, ano, DOI, veículo, autores e situação (incluído ou excluído e o motivo) |
| `triagem/triagem_automatica.csv` | resultado da triagem automática dos 1.591 registros exportados das bases (decisão, critério e justificativa por registro) |
| `triagem/selecionados_final.csv`, `.md` | estudos que passaram na checagem fina, com as bases em que aparecem |
| `triagem/excluidos_checagem.csv` | estudos excluídos na checagem fina e o motivo |
| `mapeamento_latex/extracao_dados.tsv` | extração de dados, uma linha por estudo (fonte de todos os números) |
| `mapeamento_latex/extracao_dados.xlsx` | a mesma extração formatada, com frequências e excluídos |
| `mapeamento_latex/numeros_para_texto.txt` | contagens usadas no texto |
| `mapeamento_latex/referencias_mapeamento.bib` | referências dos 53 estudos (o campo `note` traz o ID) |
| `mapeamento_latex/chaves_por_abordagem.txt` | chaves BibTeX agrupadas por abordagem (usadas nos Trabalhos Relacionados) |
| `mapeamento_latex/figuras/`, `tabelas/` | gráficos (PDF e PNG) e tabelas gerados a partir da extração |
| `mapeamento_latex/mapeamento.tex` | texto completo do mapeamento (protocolo, QP1–QP6, quadros por estudo, lacunas) |
| `mapeamento_latex/mapeamento_defesa.tex` | versão descritiva usada na dissertação final |
| `mapeamento_latex/main.tex`, `preambulo_mapeamento.tex` | documento de teste que compila só o mapeamento |

Os PDFs dos artigos não estão aqui: eles têm direitos autorais das editoras. Cada estudo pode ser
obtido pelo DOI em `corpus_final.csv`. Pelo mesmo motivo, os arquivos exportados das bases (CSV/BibTeX
brutos), os resumos dos artigos (removidos de `triagem_automatica.csv`) e os lotes de leitura não estão
incluídos. Por isso, `triagem.py` e `checagem_fina.py` só rodam de novo a partir de uma nova exportação
das bases; os resultados das duas etapas estão completos em `triagem/`.

## Como regerar

Os scripts ficam em `../scripts/mapeamento/` e leem e gravam nesta pasta:

```bash
python scripts/mapeamento/gerar_mapeamento.py      # figuras, tabelas, .bib, planilha e números a partir do .tsv
python scripts/mapeamento/gerar_figuras_defesa.py  # figuras extras da defesa e chaves por abordagem
python scripts/mapeamento/checagem_fina.py         # refaz a checagem fina a partir de triagem_automatica.csv
python scripts/mapeamento/triagem.py --pasta <pasta com os exports das bases>
```
