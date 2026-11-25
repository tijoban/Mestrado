"""Trava [0, 100]% da previsão recursiva e sinal de divergência, sem treinar rede nem tocar no banco.

  python -m unittest tests.test_trava -v
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import numpy as np

# config.py lê POSTGRES_* na importação; o teste não conecta ao banco, então bastam valores fictícios.
for _k in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "teste")

from previsao import main as job
from previsao.worker import prever_recursivo


class ModeloFalso:
    """Imita a rede Keras: devolve a próxima saída da lista (em fração) e guarda o que recebeu na janela."""

    def __init__(self, saidas):
        self.saidas, self.janelas = list(saidas), []

    def __call__(self, x, training=False):
        self.janelas.append(np.array(x).ravel().copy())
        v = self.saidas.pop(0)
        return mock.Mock(numpy=lambda: np.array([[v]], dtype="float32"))


T0, PASSO = datetime(2026, 11, 1), timedelta(days=1)


class TestTrava(unittest.TestCase):
    def test_acima_de_100(self):
        m = ModeloFalso([0.95, 1.30, 1.647, 0.90])          # a rede tenta 130% e 164,7%
        prev, max_bruto, div = prever_recursivo(m, [0.8, 0.85, 0.9], 4, T0, PASSO)
        print(f"\n  acima: previsto gravado = {prev.round(1).tolist()} | max bruto = {max_bruto:.1f}% | divergencia = {div}")
        self.assertEqual(prev.round(4).tolist(), [95.0, 100.0, 100.0, 90.0])
        self.assertAlmostEqual(max_bruto, 164.7, places=1)
        self.assertTrue(div)
        self.assertLessEqual(float(m.janelas[2][-1]), 1.0)   # a recursão recebe o valor travado, não 1,30

    def test_abaixo_de_0(self):
        m = ModeloFalso([0.05, -0.20, -0.01])
        prev, max_bruto, div = prever_recursivo(m, [0.1, 0.08, 0.06], 3, T0, PASSO)
        print(f"\n  abaixo: previsto gravado = {prev.round(1).tolist()} | max bruto = {max_bruto:.1f}% | divergencia = {div}")
        self.assertEqual(prev.round(4).tolist(), [5.0, 0.0, 0.0])
        self.assertTrue(div)
        self.assertGreaterEqual(float(m.janelas[2][-1]), 0.0)

    def test_dentro_da_faixa(self):
        prev, max_bruto, div = prever_recursivo(ModeloFalso([0.4, 0.5, 0.6]), [0.3, 0.35, 0.4], 3, T0, PASSO)
        print(f"\n  dentro: previsto gravado = {prev.round(1).tolist()} | max bruto = {max_bruto:.1f}% | divergencia = {div}")
        self.assertEqual(prev.round(4).tolist(), [40.0, 50.0, 60.0])
        self.assertFalse(div)

    def test_gravar_envia_colunas_novas(self):
        """_gravar manda max_previsto_bruto e divergencia ao INSERT (banco substituído por um dublê)."""
        r = {"n_pontos": 200, "max_previsto_lstm": 100.0, "max_previsto_bruto": 164.7, "divergencia": True,
             "pontos": [(T0.date(), "lstm", 100.0)]}
        t = {"fonte": "vcenter", "nivel": "cluster", "entidade": "Cluster B", "metrica": "memoria",
             "variante": "bruto", "freq": "1D"}
        with mock.patch.object(job, "executar", return_value=(1,)) as ex, mock.patch.object(job, "inserir_lote") as il:
            job._gravar(99, t, r)
        sql, params = ex.call_args.args
        colunas = sql.split("(execucao_id,")[1].split(")")[0].replace("\n", " ")
        nomes = ["execucao_id"] + [c.strip() for c in colunas.split(",")]
        valores = dict(zip(nomes, params))
        print(f"\n  INSERT: max_previsto_bruto = {valores['max_previsto_bruto']} | divergencia = {valores['divergencia']} "
              f"| ponto lstm gravado = {il.call_args_list[0].args[1][0][3]}")
        self.assertEqual(valores["max_previsto_bruto"], 164.7)
        self.assertIs(valores["divergencia"], True)
        self.assertEqual(il.call_args_list[0].args[1][0][3], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
