@echo off
rem Startet den Foto-Sortierer (Doppelklick genuegt)
where py >nul 2>nul && (start "" pyw -3 "%~dp0foto_sortierer.py" & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0foto_sortierer.py" & exit /b)
echo Python wurde nicht gefunden. Bitte von https://www.python.org installieren
echo (beim Installieren "Add python.exe to PATH" anhaken).
pause
