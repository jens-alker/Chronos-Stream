@echo off
echo.
echo === Ollama wird mit FlashAttention gestartet ===
echo.

echo Beende evtl. laufende Ollama-Instanz...
taskkill /F /IM ollama.exe        >nul 2>&1
taskkill /F /IM "ollama app.exe"  >nul 2>&1
REM WICHTIG (Jens 08.08., OOM-Ursache): ein abgestuerzter/haengender Modell-Load laesst eine
REM llama-server.exe zurueck, die den VRAM WEITER belegt -> der naechste 30B-Load scheitert mit
REM 'cudaMalloc failed: out of memory', obwohl das Modell an sich (17.3 GiB auf 20 GB) passt.
REM Deshalb hier auch die Modell-Subprozesse killen, damit die Karte sauber frei ist. Verschiedene
REM Ollama-Versionen benennen den Runner llama-server.exe ODER ollama_llama_server.exe (Fable-m6) -> beide.
taskkill /F /IM llama-server.exe         >nul 2>&1
taskkill /F /IM ollama_llama_server.exe  >nul 2>&1
REM 2s VRAM-Grace. `ping` statt `timeout`, weil `timeout` ohne Konsole/stdin (detached aus dem
REM Start-Knopf/Guard gestartet) sofort abbricht (Fable-m2) — `ping` wartet dort zuverlaessig.
ping -n 3 127.0.0.1 >nul

set OLLAMA_FLASH_ATTENTION=1
set OLLAMA_KV_CACHE_TYPE=q8_0
REM Nur EIN Modell gleichzeitig RESIDENT (Fable-M2, ehrlich): die prozess-uebergreifende GPU-Sperre
REM serialisiert die NUTZUNG von qwen3:30b und nomic, verhindert aber nicht deren KO-RESIDENZ im VRAM
REM (17.3 GiB + KV-Cache lassen fuer nomic kaum Luft -> sonst OOM beim Laden des zweiten Modells). =1
REM evictet das jeweils andere -> kein Ko-Residenz-OOM. PREIS: wechselt der Betrieb haeufig 1c<->Dedup,
REM wird qwen3:30b jedes Mal neu geladen (Reload-Churn, Minuten von Platte). Bei viel Wechsel + wenn der
REM Zombie-Kill oben schon reicht, kann =2 mehr Durchsatz bringen (dann aber Ko-Residenz-OOM-Risiko).
set OLLAMA_MAX_LOADED_MODELS=1
REM Kontextfenster: 8192 deckt die 1c-Dokumente (bis ~6000 Zeichen) + Prompt. Auf einer 20-GB-Karte
REM ist qwen3:30b (17.3 GiB) + KV-Cache damit knapp, aber es passt bei SAUBER freier Karte. Bei
REM wiederkehrendem OOM trotz sauberem Start: auf 6144/4096 senken (kostet etwas Extraktions-Kontext).
set OLLAMA_CONTEXT_LENGTH=8192
set OLLAMA_KEEP_ALIVE=30m

echo   OLLAMA_FLASH_ATTENTION = %OLLAMA_FLASH_ATTENTION%
echo   OLLAMA_KV_CACHE_TYPE   = %OLLAMA_KV_CACHE_TYPE%
echo   OLLAMA_CONTEXT_LENGTH  = %OLLAMA_CONTEXT_LENGTH%
echo   OLLAMA_KEEP_ALIVE      = %OLLAMA_KEEP_ALIVE%
echo.

echo Starte Ollama-Server...  (dieses Fenster offen lassen)
echo.
ollama serve