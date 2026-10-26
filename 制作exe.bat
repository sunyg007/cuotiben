@echo off
chcp 936 >nul
cd /d %~dp0
title 制作错题本.exe

echo ==========================================================
echo   错题本 - 打包 exe（面板 + 本地服务一体）
echo   产物：dist\错题本\错题本.exe（整个文件夹一起分发）
echo ==========================================================
echo.
echo  前提：本机已装 Python 和 PyInstaller（pip install pyinstaller）
echo  打包内容：面板 + server.py + web 前端 + segno 二维码库
echo  不会打进：data（题库）、config.json（API Key）—— 干净程序
echo.

pyinstaller --noconfirm --clean --onedir --windowed ^
  --name 错题本 ^
  --icon 错题本.ico ^
  --add-data "web;web" ^
  --add-data "segno;segno" ^
  --paths . ^
  面板.pyw

if errorlevel 1 (
  echo.
  echo  !! 打包失败，看上面的报错。
  pause
  exit /b 1
)

echo.
echo  ==========================================================
echo   打包完成：dist\错题本\
echo   把整个「错题本」文件夹复制给别的电脑即可用（无需装 Python）。
echo   双击 错题本.exe = 面板窗口（统计 + 二维码），关闭窗口即停服务。
echo  ==========================================================
echo.
pause
