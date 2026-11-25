# Mapeamento sistemático — arquivos LaTeX

| Arquivo/pasta | O que é |
|---|---|
| `mapeamento.tex` | Texto da seção (protocolo, QP1–QP6, lacunas, ameaças à validade) |
| `preambulo_mapeamento.tex` | Pacotes e comandos usados pela seção (`\mapquadro`, `\mapfonte`, `\mappath`) |
| `main.tex` | Documento abnTeX2 de teste que compila só o mapeamento |
| `referencias_mapeamento.bib` | Referências dos 53 estudos (campo `note` traz o ID, ex.: "IEEE 18") |
| `figuras/` | 15 gráficos em PDF (vetorial, para o LaTeX) e PNG (para Word/slides) |
| `tabelas/` | Tabelas e quadros gerados a partir da extração |
| `extracao_dados.tsv` | Extração de dados, uma linha por estudo (fonte de tudo) |
| `extracao_dados.xlsx` | A mesma extração formatada + frequências + excluídos |
| `gerar_mapeamento.py` | Regera figuras, tabelas, .bib e planilha a partir do .tsv |
| `numeros_para_texto.txt` | Contagens usadas no texto, para conferência |

## Como usar na dissertação

1. Copie a pasta `mapeamento_latex` para o projeto (ex.: Overleaf).
2. No preâmbulo:
   ```latex
   \newcommand{\mappath}{mapeamento_latex/}
   \input{mapeamento_latex/preambulo_mapeamento}
   ```
3. No capítulo: `\input{mapeamento_latex/mapeamento}`
4. Na bibliografia, inclua `mapeamento_latex/referencias_mapeamento`.
5. Compile: pdflatex → bibtex → pdflatex → pdflatex.

## Pendências

- Inserir a string de busca exata (há um `% TODO` em `mapeamento.tex`).
- IEEE 16 (MEKITformer) foi extraído só do resumo: o PDF foi apagado da pasta `artigos`.
  Se baixar de novo, salve como `IEEE 16.pdf` e ajuste a linha dele no `.tsv`.

## Corrigindo dados

Edite `extracao_dados.tsv` (tabulado, UTF-8) e rode `python gerar_mapeamento.py`.
As contagens do texto em `mapeamento.tex` são fixas: confira-as com `numeros_para_texto.txt`.
