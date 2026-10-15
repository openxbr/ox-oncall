# Settings da PoC: o settings.helm do fork + httpx/httpcore em WARNING.
#
# O httpx (usado pelo python-telegram-bot) loga em INFO a URL de cada chamada, e a URL da Bot API
# leva o token do bot: "POST https://api.telegram.org/bot<token>/getUpdates". Com o root logger
# do engine em INFO, o token ia para o log a cada getUpdates. Medido em 15/10/2026 no
# telegram-polling. Montado por bind em /etc/app/settings/openx.py (sem build de imagem).
from .helm import *  # noqa: F401,F403
from .helm import LOGGING

LOGGING["loggers"]["httpx"] = {"level": "WARNING"}
LOGGING["loggers"]["httpcore"] = {"level": "WARNING"}
