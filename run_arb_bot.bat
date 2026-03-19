@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python nao encontrado no PATH.
  echo Instale Python ou abra o terminal onde "python" funciona.
  pause
  exit /b 1
)

:menu
cls
echo ============================================================
echo   ARB BTC 15M - MENU RAPIDO
echo ============================================================
echo.
echo [1] Iniciar monitores ao vivo (Kalshi + Polymarket)
echo [2] Rodar analise dos logs (filtro minimo 5%%)
echo [3] Dry run - replay
echo [4] Dry run - fault-injection
echo [5] Dry run - live-observe
echo [6] Fluxo guiado (coleta + analise + replay)
echo [7] Dry run - pessimistic-exec (atraso + execucao)
echo [8] Production-ready plan (cap US$10 Kalshi / US$10 Poly)
echo [9] Live-Sim (CSV tail)
echo [10] Live-Shadow (feed direto WS+REST, sem ordens reais)
echo [11] Live-Prod (bloqueado por policy)
echo [0] Sair
echo.
set /p CHOICE=Escolha uma opcao: 

if "%CHOICE%"=="1" goto monitors
if "%CHOICE%"=="2" goto analyze
if "%CHOICE%"=="3" goto replay
if "%CHOICE%"=="4" goto faults
if "%CHOICE%"=="5" goto liveobserve
if "%CHOICE%"=="6" goto fullflow
if "%CHOICE%"=="7" goto pessimistic
if "%CHOICE%"=="8" goto productionready
if "%CHOICE%"=="9" goto livesim
if "%CHOICE%"=="10" goto liveshadow
if "%CHOICE%"=="11" goto liveprod
if "%CHOICE%"=="0" goto end

echo.
echo Opcao invalida.
pause
goto menu

:monitors
echo.
echo Abrindo 2 janelas de monitoramento...
start "Kalshi BTC 15M Monitor" cmd /k "title Kalshi BTC 15M Monitor && python watch_btc_15m_kalshi.py --interval 0.1"
start "Polymarket BTC 15M Monitor" cmd /k "title Polymarket BTC 15M Monitor && python watch_btc_15m_poly.py --interval 0.1"
echo Monitores iniciados. Deixe rodar alguns minutos para gerar dados.
pause
goto menu

:analyze
echo.
echo Rodando analise...
python logs\analyze_arb.py --min-edge-pct 5
echo.
echo Arquivos gerados/atualizados:
echo  - logs\arb_results.txt
echo  - logs\arb_opportunities.csv
echo  - logs\arb_diagnostics.csv
pause
goto menu

:replay
echo.
echo Rodando dry run replay...
python logs\run_arb_dry_run.py --mode replay --min-edge-pct 5 --summary-file logs\dry_run_summary.txt
echo.
echo Resumo: logs\dry_run_summary.txt
pause
goto menu

:faults
echo.
echo Rodando dry run fault-injection...
python logs\run_arb_dry_run.py --mode fault-injection --min-edge-pct 5 --summary-file logs\dry_run_summary_faults.txt
echo.
echo Resumo: logs\dry_run_summary_faults.txt
pause
goto menu

:liveobserve
echo.
set /p DURATION=Duracao em segundos para live-observe [60]: 
if "%DURATION%"=="" set DURATION=60
echo Rodando dry run live-observe por %DURATION%s...
python logs\run_arb_dry_run.py --mode live-observe --live-duration-sec %DURATION% --live-interval-sec 0.5 --min-edge-pct 5 --summary-file logs\dry_run_summary_live.txt
echo.
echo Resumo: logs\dry_run_summary_live.txt
pause
goto menu

:pessimistic
echo.
set /p PESS_DELAY=Delay pessimista entre pernas em segundos [5]: 
if "%PESS_DELAY%"=="" set PESS_DELAY=5
set /p PESS_MIN_EDGE=Edge minimo em %% para validar oportunidade [5]: 
if "%PESS_MIN_EDGE%"=="" set PESS_MIN_EDGE=5
echo Rodando dry run pessimistic-exec...
python logs\run_arb_dry_run.py --mode pessimistic-exec --pess-delay-sec %PESS_DELAY% --min-edge-pct %PESS_MIN_EDGE% --summary-file logs\dry_run_summary_pessimistic.txt
echo.
echo Resumo: logs\dry_run_summary_pessimistic.txt
echo CSV de estresse: logs\arb_pessimistic_exec_YYYYMMDD_HHMMSS.csv
pause
goto menu

:productionready
echo.
set /p PROD_DELAY=Delay pessimista entre pernas em segundos [3]: 
if "%PROD_DELAY%"=="" set PROD_DELAY=3
set /p PROD_EDGE=Edge minimo de producao em %% [7]: 
if "%PROD_EDGE%"=="" set PROD_EDGE=7
set /p PROD_K=Budget Kalshi em US$ [10]: 
if "%PROD_K%"=="" set PROD_K=10
set /p PROD_P=Budget Poly em US$ [10]: 
if "%PROD_P%"=="" set PROD_P=10
set /p PROD_TTE=Minimo de segundos ate vencimento [180]: 
if "%PROD_TTE%"=="" set PROD_TTE=180
echo Rodando production-ready plan...
python logs\run_arb_dry_run.py --mode production-ready --pess-delay-sec %PROD_DELAY% --min-edge-pct 5 --prod-min-edge-pct %PROD_EDGE% --prod-max-usd-kalshi %PROD_K% --prod-max-usd-poly %PROD_P% --prod-min-tte-sec %PROD_TTE% --summary-file logs\dry_run_summary_production.txt
echo.
echo Resumo: logs\dry_run_summary_production.txt
echo Plano de ordens: logs\arb_production_plan_YYYYMMDD_HHMMSS.csv
pause
goto menu

:livesim
echo.
set /p LSIM_DUR=Duracao do Live-Sim em segundos [120]: 
if "%LSIM_DUR%"=="" set LSIM_DUR=120
echo Rodando Live-Sim (CSV tail)...
python logs\run_arb_dry_run.py --mode live-sim --runtime-sec %LSIM_DUR% --min-edge-pct 5 --summary-file logs\dry_run_summary_live_sim.txt
echo.
echo Resumo: logs\dry_run_summary_live_sim.txt
pause
goto menu

:liveshadow
echo.
set /p LSHADOW_DUR=Duracao do Live-Shadow em segundos [120]: 
if "%LSHADOW_DUR%"=="" set LSHADOW_DUR=120
set /p LSHADOW_EDGE=Edge minimo %% (conservador) [5]: 
if "%LSHADOW_EDGE%"=="" set LSHADOW_EDGE=5
echo Rodando Live-Shadow (feed direto, sem ordens reais)...
python logs\run_arb_dry_run.py --mode live-shadow --runtime-sec %LSHADOW_DUR% --min-edge-pct %LSHADOW_EDGE% --max-usd-kalshi 10 --max-usd-poly 10 --max-open-trades 1 --post-only-strict true --nonce-guard on --nonce-guard-action alert --summary-file logs\dry_run_summary_live_shadow.txt
echo.
echo Resumo: logs\dry_run_summary_live_shadow.txt
echo Trades: logs\arb_live_trades.csv
echo Decisoes: logs\arb_live_decisions.csv
echo Seguranca: logs\arb_live_security.csv
pause
goto menu

:liveprod
echo.
echo Live-Prod esta bloqueado por policy (PRD pausado).
echo Para validar o pipeline, use a opcao [10] Live-Shadow.
python logs\run_arb_dry_run.py --mode live-prod --summary-file logs\dry_run_summary_live_prod_locked.txt
echo.
echo Resumo: logs\dry_run_summary_live_prod_locked.txt
pause
goto menu

:fullflow
echo.
echo PASSO 1/4 - Iniciando monitores...
start "Kalshi BTC 15M Monitor" cmd /k "title Kalshi BTC 15M Monitor && python watch_btc_15m_kalshi.py --interval 0.1"
start "Polymarket BTC 15M Monitor" cmd /k "title Polymarket BTC 15M Monitor && python watch_btc_15m_poly.py --interval 0.1"
echo.
echo Monitores abertos. Deixe rodar alguns minutos.
echo Depois volte aqui e pressione uma tecla para continuar.
pause

echo.
echo PASSO 2/4 - Rodando analise...
python logs\analyze_arb.py --min-edge-pct 5

echo.
echo PASSO 3/4 - Rodando dry run replay...
python logs\run_arb_dry_run.py --mode replay --min-edge-pct 5 --summary-file logs\dry_run_summary.txt

echo.
echo PASSO 4/4 - Rodando dry run pessimistic-exec...
python logs\run_arb_dry_run.py --mode pessimistic-exec --pess-delay-sec 5 --min-edge-pct 5 --summary-file logs\dry_run_summary_pessimistic.txt

echo.
echo Fluxo concluido.
echo Veja:
echo  - logs\arb_results.txt
echo  - logs\arb_opportunities.csv
echo  - logs\arb_diagnostics.csv
echo  - logs\dry_run_summary.txt
echo  - logs\dry_run_summary_pessimistic.txt
echo Dica: use opcao [8] para gerar plano production-ready com cap de banca.
pause
goto menu

:end
echo Encerrando.
exit /b 0
