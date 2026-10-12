@echo off
title Pharmacy Invoice Review - WEB SERVER (leave this window open)
cd /d "C:\Users\joel scher\downloads\claude"
rem Go-live: the first real record is 2026-08-07. Pins statement reconciliation's cutoff so it
rem never flags invoices dated before the system started (and can't drift off the first run).
set "STATEMENT_GOLIVE_DATE=2026-08-07"
echo ================================================================
echo   Starting the Pharmacy Invoice Review web server...
echo   Keep THIS window open while you or colleagues use the site.
echo   Close it (or press Ctrl+C) to stop the site.
echo ================================================================
echo.
python run_web.py
echo.
echo Server stopped. Press any key to close this window.
pause >nul
