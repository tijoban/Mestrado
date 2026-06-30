"""
run_previsao.py
Executa previsao_novo.py para Baseline e CNN em sequência.
Coloque na mesma pasta que previsao_novo.py e env.py.
"""

import subprocess
import sys
import time
from datetime import datetime

def rodar(modo):
    print(f"\n{'═'*60}")
    print(f"  INICIANDO: {modo.upper()}")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'═'*60}\n")

    inicio = time.time()

    resultado = subprocess.run(
        [sys.executable, "previsao_novo.py", "--modo", modo],
        cwd=__file__.replace("run_previsao.py", "")
    )

    fim = round((time.time() - inicio) / 60, 1)

    if resultado.returncode == 0:
        print(f"\n  ✓ {modo.upper()} concluído em {fim} min")
    else:
        print(f"\n  ✗ {modo.upper()} falhou após {fim} min")

    return resultado.returncode


if __name__ == "__main__":
    inicio_total = time.time()
    print(f"\nInício: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")

    cod_baseline = rodar("baseline")
    cod_cnn      = rodar("cnn")

    total = round((time.time() - inicio_total) / 60, 1)

    print(f"\n{'═'*60}")
    print(f"  RESUMO FINAL")
    print(f"{'═'*60}")
    print(f"  Baseline : {'OK' if cod_baseline == 0 else 'FALHOU'}")
    print(f"  CNN      : {'OK' if cod_cnn      == 0 else 'FALHOU'}")
    print(f"  Tempo total: {total} min")
    print(f"  Fim: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'═'*60}")