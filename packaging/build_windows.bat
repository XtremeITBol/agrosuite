@echo off
REM ===================================================================
REM  Compila AgroSuite.exe para Windows.
REM  Requiere Python 3.11 o 3.12 de 64 bits instalado y en el PATH.
REM  Ejecutar desde la raiz del proyecto:  packaging\build_windows.bat
REM ===================================================================
setlocal

echo.
echo  [1/4] Creando entorno virtual limpio...
if exist build-venv rmdir /s /q build-venv
python -m venv build-venv || goto :error
call build-venv\Scripts\activate.bat || goto :error

echo.
echo  [2/4] Instalando dependencias...
python -m pip install --upgrade pip --quiet || goto :error
python -m pip install -r requirements.txt --quiet || goto :error
python -m pip install pyinstaller --quiet || goto :error

echo.
echo  [3/4] Empaquetando (tarda varios minutos)...
pyinstaller --clean --noconfirm packaging\agrosuite.spec || goto :error

echo.
echo  [4/4] Listo.
echo.
echo  El programa quedo en:  dist\AgroSuite\AgroSuite.exe
echo  Para distribuirlo, comprimi la carpeta  dist\AgroSuite  completa.
echo  El .exe solo NO funciona: necesita los archivos que lo acompanan.
echo.
goto :eof

:error
echo.
echo  ERROR: la compilacion fallo. Revisa el mensaje de arriba.
exit /b 1
