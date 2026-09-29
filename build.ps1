# Builds NL2Bridge.dll and NL2BridgeInjector.exe into dist\ with MinGW-w64 (MSYS2: pacman -S mingw-w64-x86_64-gcc).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Get-Command g++ -ErrorAction SilentlyContinue) -and (Test-Path C:\msys64\mingw64\bin\g++.exe)) {
    $env:PATH = "C:\msys64\mingw64\bin;$env:PATH"
}
New-Item -ItemType Directory -Force dist | Out-Null
g++ -O2 -std=c++17 -shared -static -Wall -Wno-unused-function -o dist\NL2Bridge.dll src\nl2bridge.cpp -lws2_32 -lpsapi
if ($LASTEXITCODE) { throw "DLL build failed" }
g++ -O2 -municode -static -o dist\NL2BridgeInjector.exe src\injector.cpp
if ($LASTEXITCODE) { throw "Injector build failed" }
Get-ChildItem dist | Format-Table Name, Length
