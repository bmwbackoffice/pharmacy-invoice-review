@echo off
title Fetch + Classify New Invoices
cd /d "C:\Users\joel scher\downloads\claude"
rem Go-live: the first real record is 2026-08-07. Pins statement reconciliation's cutoff so it
rem never flags invoices dated before the system started (and can't drift off the first run).
set "STATEMENT_GOLIVE_DATE=2026-08-07"
echo ================================================================
echo   Fetching new emails from Gmail and classifying them...
echo   (This writes to the database; the web UI will show them
echo    after you refresh your browser.)
echo ================================================================
echo.
python run_all.py --drive
echo.
echo ----------------------------------------------------------------
echo   Done. Refresh the web UI in your browser to see new items.
echo ----------------------------------------------------------------
pause
