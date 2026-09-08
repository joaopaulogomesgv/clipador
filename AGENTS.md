# AGENTS.md - Clipador (Cortador Inteligente de Vídeos com IA)

Guia completo de arquitetura, padrões técnicos e diretrizes para agentes e desenvolvedores que mantêm este repositório.

---

## 1. Visão Geral do Projeto

O **Clipador** é uma aplicação completa para corte automático e curadoria de vídeos longos (podcasts, entrevistas, lives) em cortes curtos de alta retenção voltados para **TikTok, Instagram Reels e YouTube Shorts**.

O sistema combina inteligência de áudio local (**OpenAI Whisper**), curadoria contextual via **Google Gemini API**, fatiamento preciso com **FFmpeg** e uma interface web reativa em **Vanilla JS/HTML/CSS** com persistência total de estado.

---

## 2. Stack Tecnológico

* **Backend**: Python 3.8+ (Flask 3.x)
* **Frontend**: Single Page Application (SPA) em Vanilla HTML5, CSS3 Moderno (Dark Mode Glassmorphism) e JavaScript ES6+
* **Motor de IA & NLP**:
  * **OpenAI Whisper (`tiny` / `base`)**: Transcrição local de áudio para texto com timestamps por segmento.
  * **Google Gemini API (`gemini-3.6-flash` / `gemini-flash-latest`)**: Análise de narrativa completa, classificação de potencial viral (0 a 100), geração de títulos chamativos e extração de ganchos (hooks).
* **Processamento Audiovisual**:
  * **FFmpeg**: Extração de áudio mono PCM 16kHz, detecção acelerada de trocas de cena (`scale=320:-1`) e exportação precisa de vídeo H.264 + áudio AAC (`-avoid_negative_ts make_zero`).
  * **OpenCV (cv2) & NumPy**: Análise visual de frames e vetorização matemática de decibéis para detecção de silêncio ultrarrápida.
* **Download de Conteúdo**:
  * **yt-dlp**: Download de vídeos do YouTube com hooks de progresso em tempo real.

---

## 3. Estrutura do Repositório

```
clipador/
├── AGENTS.md                       # Diretrizes do projeto para agentes IA (este arquivo)
├── .gitignore                      # Ignora uploads, exports, venv, cache e config.json
└── video_cutter_web/               # Código principal da aplicação
    ├── app.py                      # Servidor Flask e rotas da API REST
    ├── run.bat                     # Script de inicialização padrão no Windows
    ├── requirements.txt            # Dependências Python
    ├── config.json                 # Configurações locais e chaves de API (ignorado no git)
    ├── core/                       # Módulos de lógica central e IA
    │   ├── gemini_analyzer.py      # Integração com Gemini API e alinhamento de ganchos
    │   └── hook_analyzer.py        # Reconstrução sentencial e heurísticas locais de corte
    ├── templates/
    │   └── index.html              # Interface do usuário completa (SPA)
    ├── static/                     # Assets estáticos (CSS, JS, ícones)
    ├── uploads/                    # Vídeos enviados e caches ({id}_transcription.json)
    └── exports/                    # Clipes MP4 renderizados e arquivos cortes.zip
```

---

## 4. Componentes Principais e Arquitetura

### 4.1. Motor de Alinhamento Sentencial e Ganchos (`core/hook_analyzer.py` e `core/gemini_analyzer.py`)
* **`reconstruct_sentences(whisper_segments)`**: O Whisper fatia a fala em fragmentos curtos de 1 a 2 segundos com gagueiras ou pontuações incompletas. Esta função agrupa os fragmentos em **frases e orações completas** com base em pontuação e pausas naturais (> 1.2s), atribuindo um `id` sequencial a cada uma.
* **`align_cut_to_sentences(raw_cut, sentences, total_duration)`**:
  * **Problema Resolvido**: LLMs frequentemente selecionam timestamps deslocados (ex: pegando o timestamp final da frase do gancho em vez do inicial), o que cortava o gancho fora do vídeo.
  * **Solução**: O algoritmo rastreia o gancho na transcrição e força o início do clipe (`start`) a começar exatamente no início da frase de impacto.
  * **Inclusão da Pergunta do Apresentador**: Se a frase de início for precedida de uma pergunta curta (< 8,5s), o corte recua para incluir a pergunta, garantindo que o vídeo faça sentido completo.
  * **Margens Sonoras de Respiro**: Adiciona -0,35s no início e +0,4s no final para evitar que a primeira consoante ou última sílaba sejam comidas pelo codec de áudio.

### 4.2. Cache Permanente de Transcrição (Fast-Path no `app.py`)
* Em vídeos longos (ex: 2h20), o Whisper leva ~30 minutos para transcrever em CPU.
* Assim que a transcrição é concluída pela primeira vez, o array completo é persistido em `uploads/{video_id}_transcription.json`.
* Em análises subsequentes ou ao recalcular ganchos, o sistema carrega o arquivo do disco em **0,05s**, pulando toda a etapa pesada e gerando os cortes em **menos de 3 segundos**.

### 4.3. Persistência de Sessão no Frontend (`templates/index.html`)
* **`restoreActiveSession()`**: Conectado ao `localStorage` (`clipador_active_video_id` e `clipador_active_analysis_id`).
* Ao recarregar a página (`F5`), o sistema restaura automaticamente:
  1. O reprodutor de vídeo e metadados (duração, resolução, codec).
  2. A lista de cortes gerados com seus respectivos scores e status de seleção.
  3. O banner de download com o botão de download do ZIP se já houver arquivos renderizados.
  4. Reconexão em tempo real à barra de progresso caso uma análise estivesse em andamento durante o reload.
* **`resetCurrentVideo()`**: Botão *"Trocar Vídeo"* adicionado na barra do player para limpar a sessão caso o usuário queira carregar outro arquivo.

### 4.4. Renderização e Exportação Seletiva
* A rota `/export/start/<video_id>` aceita `selected_ids`. Se o usuário selecionar apenas 3 ou 5 cortes, apenas esses são renderizados via FFmpeg e compactados no `cortes.zip`.
* Renderização multithread com `ThreadPoolExecutor` utilizando re-encoding ultrarrápido (`-c:v libx264 -preset ultrafast -crf 22 -c:a aac -b:a 192k`).

---

## 5. Endpoints da API REST

| Método | Rota | Descrição |
| :--- | :--- | :--- |
| `GET` | `/` | Carrega a interface SPA principal |
| `POST` | `/upload` | Upload de arquivo de vídeo multipart/form-data |
| `GET` | `/video/<video_id>` | Streaming do arquivo de vídeo para o player |
| `GET` | `/api/video/<video_id>` | Retorna dados da sessão (metadados, cortes salvos, transcrição) |
| `GET` | `/api/videos/latest` | Retorna a sessão do vídeo mais recente |
| `POST` | `/analyze/<video_id>` | Inicia análise de vídeo (retorna `analysis_id`) |
| `GET` | `/analyze/progress/<analysis_id>` | Polling do percentual e etapa da análise |
| `GET` | `/analyze/result/<analysis_id>` | Retorna os cortes gerados após conclusão |
| `POST` | `/reanalyze_hooks/<video_id>` | Recalcula ganchos e cortes instantaneamente via cache |
| `POST` | `/export/start/<video_id>` | Inicia a renderização dos cortes selecionados em segundo plano |
| `GET` | `/export/status/<video_id>` | Polling do progresso de exportação e geração do ZIP |
| `GET` | `/export/download/<video_id>` | Download do pacote `cortes.zip` |
| `GET` | `/export/file/<video_id>/<filename>` | Download de um corte individual `.mp4` |
| `GET` | `/api/config` | Consulta se há chave do Gemini configurada |
| `POST` | `/api/config` | Salva a chave de API e modelo do Gemini em `config.json` |
| `POST` | `/api/config/test_gemini` | Testa a conectividade da chave do Gemini |
| `POST` | `/yt/info` | Extrai título, duração e thumbnail de URL do YouTube |
| `POST` | `/yt/download` | Inicia download de vídeo do YouTube via `yt-dlp` |
| `GET` | `/yt/progress/<download_id>` | Polling do download do YouTube |
| `GET` | `/yt/file/<download_id>` | Download do arquivo baixado do YouTube |

---

## 6. Configurações e Chaves de API (`config.json`)

O arquivo `video_cutter_web/config.json` armazena as preferências do usuário e é ignorado no controle de versão:
```json
{
  "use_gemini": true,
  "gemini_model": "gemini-3.6-flash",
  "gemini_api_key": "SUA_CHAVE_AQUI"
}
```

* Modelos homologados: `gemini-3.6-flash` (padrão de alta velocidade) e `gemini-flash-latest`.
* O cliente HTTP em `gemini_analyzer.py` utiliza `urllib.request.build_opener(urllib.request.ProxyHandler({}))` para contornar lentidão de autodescoberta de proxy no Windows (WPAD).

---

## 7. Instruções para Desenvolvedores e Agentes

1. **Nunca quebre o Alinhamento de Ganchos**: Ao modificar a lógica de cortes, certifique-se de que a função `align_cut_to_sentences` permaneça ativa para validar os timestamps antes de salvar os cortes em disco.
2. **Preserve a Persistência de Sessão**: Qualquer novo parâmetro de corte adicionado ao frontend deve ser salvo na sessão ou restaurado via `restoreActiveSession`.
3. **Segurança de Chaves de API**: Nunca comite chaves de API ou dados de vídeos em commits do Git. O `.gitignore` está configurado para bloquear `config.json`, `uploads/` e `exports/`.
