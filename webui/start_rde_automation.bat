@echo off
title RDE Automation Server

cd /d "C:\Users\zyang\Downloads\RDE data\RDE\webui"

"%LOCALAPPDATA%\miniforge3\python.exe" .\server_awake.py

pause
