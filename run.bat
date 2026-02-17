@echo off
echo Starting LMArena Client...

:: Activate venv if it exists
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

:: Run streamlit
streamlit run lmarena_app.py --server.port 8501 --server.headless false --browser.gatherUsageStats false

pause
