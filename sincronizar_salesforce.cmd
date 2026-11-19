@echo off
rem Sincronizacao automatica com o Salesforce (somente leitura) - uso local no Windows.
rem Na VPS o mesmo comando roda pelo systemd timer (ver docs\DEPLOY_VPS.md).
cd /d "%~dp0"
if not exist logs mkdir logs
echo ==== %date% %time% >> logs\sincronizar_salesforce.log
python manage.py sincronizar_automatico >> logs\sincronizar_salesforce.log 2>&1
