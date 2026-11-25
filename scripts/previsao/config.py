import os

# Banco (secret capacityplan-database-config, mesma entrada do config-manager).
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
POSTGRES_USER     = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

# Esgotamento: percentual em que a métrica é considerada esgotada (defesa: 80%).
LIMIAR_PCT = float(os.environ.get("LIMIAR_PCT", "80"))

# Janela de entrada do LSTM (pontos): 1 dia em 30 min; 14 dias na série diária.
LOOK_BACK = {"30min": 48, "1D": 14}
HOLD_OUT = 0.15

# Trecho previsto pelo LSTM+GRU (recursivo). Depois dele a curva segue pela
# tendência linear da série até cruzar o limiar.
HORIZONTE_LSTM_DIAS = int(os.environ.get("HORIZONTE_LSTM_DIAS", "30"))
# Janela usada para medir a tendência (dias mais recentes da série).
JANELA_TENDENCIA_DIAS = int(os.environ.get("JANELA_TENDENCIA_DIAS", "90"))
# Limite da projeção: além disso a métrica é marcada como "não esgota".
HORIZONTE_MAX_ANOS = int(os.environ.get("HORIZONTE_MAX_ANOS", "10"))

# Épocas máximas por série (EarlyStopping costuma parar antes).
EPOCAS = int(os.environ.get("EPOCAS", "40"))

# Mínimo de pontos para treinar uma série.
MIN_PONTOS = {"30min": 48 * 14, "1D": 60}

# Execuções simultâneas (cada uma é um subprocesso com o próprio TensorFlow).
PARALELO = int(os.environ.get("PARALELO", "2"))
# Limita as fontes/entidades (útil para testar): "vcenter,nutanix,clientes".
FONTES = [f.strip() for f in os.environ.get("FONTES", "vcenter,nutanix,clientes").split(",") if f.strip()]
NIVEIS = [n.strip() for n in os.environ.get("NIVEIS", "total,cluster,host,cliente").split(",") if n.strip()]
MAX_SERIES = int(os.environ.get("MAX_SERIES", "0"))  # 0 = sem limite
# Id de uma execução interrompida: continua nela, pulando as séries já gravadas sem erro.
RETOMAR = int(os.environ.get("RETOMAR", "0"))

# Histórico diário gravado para o gráfico.
DIAS_HISTORICO_GRAFICO = 180

# Nutanix para o estudo: só até 25/08/2026. Em 26–27/08 as VMs foram
# migradas do cluster antigo para o cluster novo, o que derruba o uso
# (quebra estrutural, não consumo). O cluster novo ainda não tem histórico
# válido no db_nutanix (cluster novo e hosts do cluster novo zerados).
NUTANIX_ATE = os.environ.get("NUTANIX_ATE", "2026-08-25")  # inclusive; "" = sem corte
NUTANIX_EXCLUIR = [e.strip() for e in os.environ.get(
    "NUTANIX_EXCLUIR", "").split(",") if e.strip()]

# Motivo do "não esgota": |tendência| abaixo disto (pontos percentuais/dia) = estável.
TENDENCIA_ESTAVEL_PP_DIA = float(os.environ.get("TENDENCIA_ESTAVEL_PP_DIA", "0.01"))
