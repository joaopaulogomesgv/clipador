# AGENTS.md - Clipador (Cortador de Videos Automatico)

## Resumo do Projeto

Aplicacao web para corte automatico de videos, construida com Flask (Python) + vanilla HTML/CSS/JS.
Analisa videos grandes e cria cortes automaticos baseados em deteccao de silencio audio + mudancas de cena.

## Stack Tecnico

- **Backend**: Python Flask (arquivo unico `app.py`, ~900 linhas)
- **Frontend**: HTML/CSS/JS vanilla (arquivo unico `templates/index.html`, ~1900 linhas)
- **Analise de video**: OpenCV (cv2) + NumPy
- **Download YouTube**: yt-dlp
- **Audio**: wave + struct + NumPy (quando FFmpeg disponivel)
- **Formatos suportados**: MP4, MKV, AVI, MOV, WebM

## Estrutura de Diretorios

```
video_cutter_web/
├── app.py              # Backend Flask completo
├── requirements.txt    # flask, opencv-python, numpy, yt-dlp
├── templates/
│   └── index.html      # Frontend completo (SPA com sidebar)
├── uploads/            # Videos carregados
│   └── yt_downloads/   # Downloads do YouTube
└── exports/            # Cortes exportados
```

## Endpoints da API

### Upload e Video
- `GET /` - Pagina principal
- `POST /upload` - Upload de video (retorna video_id + info)
- `GET /video/<video_id>` - Servir video para o player

### Analise
- `POST /analyze/<video_id>` - Iniciar analise (retorna analysis_id)
- `GET /analyze/progress/<analysis_id>` - Polling de progresso
- `GET /analyze/result/<analysis_id>` - Resultado da analise

### Exportacao
- `POST /export/<video_id>` - Exportar cortes (retorna arquivo MP4 ou ZIP)

### YouTube
- `POST /yt/info` - Buscar info do video
- `POST /yt/download` - Iniciar download (retorna download_id)
- `GET /yt/progress/<download_id>` - Polling de progresso
- `GET /yt/file/<download_id>` - Download do arquivo

## Funcionalidades Implementadas

1. **Upload com drag & drop** + progresso de upload
2. **Player de video** com controles (play/pause, seek, timeline)
3. **Deteccao de cenas** usando OpenCV (mudanca visual entre frames)
4. **Analise de silencio** - 2 modos:
   - Com FFmpeg: extrai audio WAV, analisa dB com wave+numpy
   - Sem FFmpeg: usa movimento visual como proxy (OpenCV)
5. **Combinacao de resultados** - junta silencio + cenas para gerar cortes
6. **Exportacao** - MP4 individual ou pacote ZIP
7. **YouTube Downloader** - com progresso em tempo real
8. **UI Dashboard** - sidebar com navegacao, stats cards, configuracoes

## Estado Atual e Problemas

### Ambiente
- **Python 3.14** instalado
- **FFmpeg NAO disponivel** (`where ffmpeg` retorna erro)
- **pydub nao funciona** (audioop removido no Python 3.14)
- App roda em `http://localhost:5000`

### Problema Critico: Analise lenta em videos grandes
- Video de teste: MKV, 43 minutos, 1080p
- A funcao `analyze_silence_opencv()` estava travando em ~10-15%
- **Causa**: `cap.set(cv2.CAP_PROP_POS_FRAMES, i)` (seek aleatorio) e extremamente lento em MKVs
- **Solucao implementada**: Mudou para leitura sequencial com skip baseado em intervalo

### Configuracao Atual das Funcoes de Analise

```python
# analyze_silence_opencv()
sample_interval = max(1, int(fps * 10))  # 1 frame a cada 10 segundos
# Usa leitura SEQUENCIAL (não seek aleatorio)
# Resolucao: 64x48

# detect_scenes()
sample_interval = max(1, int(fps * 2))   # 1 frame a cada 2 segundos
# Mesma abordagem sequencial
# Resolucao: 64x48
```

### O que falta / Proximos Passos

1. **Testar se a analise agora funciona** - o servidor esta rodando, precisa testar com o video de 43min
2. **Se ainda travar**, aumentar sample_interval (ex: fps*30 para silencio, fps*5 para cenas)
3. **Extrair audio com ffmpeg-python** como alternativa ao subprocess
4. **Instalar FFmpeg** no Windows para ter analise de audio real (muito mais precisa)
5. **Adicionar waveform/visualizacao** da timeline de cortes no player
6. **Exportacao em background** com progresso (atualmente e sincrona)
7. **Exportacao sem FFmpeg** - cut_video_opencv funciona mas nao tem audio

## Comandos para Rodar

```bash
cd C:\Users\PC\Documents\clipador\video_cutter_web

# Instalar dependencias
pip install flask opencv-python numpy yt-dlp

# Rodar
python app.py

# Acessar
# http://localhost:5000
```

## Notas Importantes para Continuacao

1. **O app usa threading** para analise e download em background
2. **Progresso e feito via polling** (300ms interval no frontend)
3. **FFmpeg detection**: usa `where ffmpeg` no Windows
4. **State em memoria**: videos, cuts e progresso ficam em dicionarios Python (nao persiste entre reinicios)
5. **Max upload**: 4GB (`MAX_CONTENT_LENGTH`)
6. **Frontend e uma SPA** com 5 paginas: Dashboard, Player, Cortes, YouTube, Config
7. **switchPage()** usa onclick inline no HTML (cuidado ao modificar)
8. **Template e Jinja2** mas nao usa variavei Jinja - e puro JS vanilla

## Arquivos Importantes

- `C:\Users\PC\Documents\clipador\video_cutter_web\app.py` - Codigo backend completo
- `C:\Users\PC\Documents\clipador\video_cutter_web\templates\index.html` - Frontend completo
- `C:\Users\PC\Documents\clipador\video_cutter_web\uploads\` - Videos carregados aqui
