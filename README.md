# Stripchat Recorder

Gravador e monitor de transmissões do Stripchat via captura de stack de mídia com Chromium headless e CDP (Chrome DevTools Protocol), com remux e codificação automática para MP4 (H.264/AAC, 30fps CFR) e bot integrado do Telegram.

## Requisitos

- Python 3.10+
- FFmpeg e FFprobe
- Chromium ou Google Chrome

## Instalação

O script utiliza apenas módulos da biblioteca padrão do Python (sem dependências pip externas obrigatórias).

Certifique-se de ter o FFmpeg e o Chromium instalados:
```bash
sudo apt update && sudo apt install -y ffmpeg chromium-browser
```

## Uso

### 1. Linha de Comando (CLI)

Gravar uma transmissão diretamente:
```bash
# Gravação com duração padrão (300 segundos)
python3 st.py https://es.stripchat.com/username

# Definindo duração personalizada e nome de arquivo
python3 st.py https://es.stripchat.com/username --max-seconds 120 --out meu_video
```

### 2. Modo Bot do Telegram

Inicie o bot fornecendo o token do Telegram:
```bash
# Via argumento de linha de comando:
python3 st.py --telegram --token "SEU_TELEGRAM_BOT_TOKEN"

# Ou via variável de ambiente:
export TELEGRAM_BOT_TOKEN="SEU_TELEGRAM_BOT_TOKEN"
python3 st.py --telegram
```

#### Comandos no Telegram:
- `/model <link_stripchat> [tempo_segundos]` — Grava a transmissão silenciosamente e envia o vídeo diretamente no chat com suporte a streaming progressivo.
- `/help` ou `/start` — Exibe as instruções básicas de uso.

Exemplo no chat:
```text
/model https://es.stripchat.com/username 120
```
