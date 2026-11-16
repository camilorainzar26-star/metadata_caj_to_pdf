@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0caj_to_pdf.py" %*
    goto :done
)
python "%~dp0caj_to_pdf.py" %*
:done
if errorlevel 1 pause
