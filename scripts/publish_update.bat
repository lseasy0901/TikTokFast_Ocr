@echo off
setlocal

title LiveLens Delta Update Publisher

echo.
echo ==========================================
echo LiveLens Delta Update Publisher
echo Delta Only
echo ==========================================
echo.

set /p ver=Version (example 2.1.0):

if "%ver%"=="" (
    echo Version empty
    pause
    exit /b 1
)

set /p pkg=Delta nupkg path:


if not exist "%pkg%" (
    echo ERROR: file not found
    pause
    exit /b 1
)


echo.
echo Checking package...


powershell -NoProfile -Command ^
"$p='%pkg%'; $v='%ver%'; $i=Get-Item -LiteralPath $p; $expect='LiveLensDesktop-'+$v+'-stable-delta.nupkg'; if($i.Name -ne $expect){Write-Host 'ERROR filename mismatch'; exit 1}; Write-Host ('Package OK: '+$i.Name); Write-Host ('Size: '+$i.Length+' bytes')"


if errorlevel 1 (
    pause
    exit /b 1
)


echo.
echo Hash:


powershell -NoProfile -Command ^
"$p='%pkg%'; Write-Host ('SHA256: '+(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower())"


echo.
echo ==========================================
echo READY
echo Version: %ver%
echo File:
echo %pkg%
echo ==========================================

pause