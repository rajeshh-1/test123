@echo off
echo Iniciando Monitores de BTC 15M (Kalshi e Polymarket) com intervalo de 0.1s...

start "Kalshi BTC 15M Monitor" cmd /k "title Kalshi BTC 15M Monitor && python watch_btc_15m_kalshi.py --interval 0.1"

start "Polymarket BTC 15M Monitor" cmd /k "title Polymarket BTC 15M Monitor && python watch_btc_15m_poly.py --interval 0.1"

echo.
echo Duas janelas foram abertas separadamente para voce acompanhar os logs visuais em tempo real.
echo Feche as janelas para interromper os scripts (ou aperte Ctrl+C dentro delas).
pause
