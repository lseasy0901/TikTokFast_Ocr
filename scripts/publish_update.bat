@echo off
rem =====================================================================
rem  LiveLens Update Publisher  (MVP)
rem
rem  The single manual release entry point: local .nupkg -> validate ->
rem  hash -> call the release API.
rem
rem  Design notes:
rem    * The operator only types: version / nupkg path / share URL / notes.
rem      package_filename, SHA1, SHA256 and size are ALL derived from the
rem      local file. None of them is ever typed by hand.
rem    * Every prompt can also be supplied as an option, so the tool can be
rem      driven non-interactively (tests / CI):
rem         publish_update.bat --version 1.1.1 --nupkg PATH --share URL
rem                            --notes TEXT [--yes] [--no-key-prompt]
rem    * Hashing and size come from Windows' own PowerShell
rem      (Get-FileHash / Get-Item). No Python, Git or OpenSSL required.
rem    * Parameters reach PowerShell through ENVIRONMENT VARIABLES, so user
rem      input is never spliced into a command line or into the JSON.
rem    * The API key travels only in the HTTP header; it is never written to
rem      the temporary JSON, and it is typed without echo.
rem    * THIS FILE MUST BE PURE ASCII. Non-ASCII bytes inside rem comments
rem      get mis-paired by the console code page, which splits those lines
rem      and makes cmd try to execute the fragments.
rem    * THIS FILE MUST KEEP CRLF LINE ENDINGS. cmd.exe mis-parses LF-only
rem      batch files.
rem    * No pipe ("a|b") anywhere in this script. A pipe in a batch file
rem      spawns a child cmd that swallows the remaining stdin. Validation
rem      therefore happens inside PowerShell, which writes its verdict to a
rem      result file that cmd reads back with for /f (reading a FILE is safe;
rem      reading a command's output is not).
rem    * Temporary files live in %TEMP% and are always removed.
rem
rem  Exit codes: 0 ok / 1 input validation failed / 2 bad share URL /
rem              3 missing admin key / 4 cancelled by user /
rem              5 hashing failed / 6 publish request failed / 9 bad option
rem =====================================================================

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title LiveLens Update Publisher

set "LL_TMP_RESULT=%TEMP%\livelens_pub_%RANDOM%%RANDOM%.result"
set "LL_TMP_JSON=%TEMP%\livelens_pub_%RANDOM%%RANDOM%.json"

rem ---- Optional options: anything given here skips its prompt ----
call :parse_args %*
if errorlevel 1 ( endlocal & exit /b 9 )

rem ---- Server address: reuse the existing DLV_LICENSE_SERVER_URL override ----
set "LL_API_BASE=%DLV_LICENSE_SERVER_URL%"
if not defined LL_API_BASE set "LL_API_BASE=https://livelen.icu/api/v1"
if "%LL_API_BASE:~-1%"=="/" set "LL_API_BASE=%LL_API_BASE:~0,-1%"
set "LL_API_URL=%LL_API_BASE%/updates/releases"

echo ========================================
echo   LiveLens Update Publisher
echo ========================================
echo.
echo API: %LL_API_URL%
echo.

rem ---------------------------------------------------------------------
rem 1) Collect whatever was not given as an option.
rem ---------------------------------------------------------------------
if not defined LL_VER set /p "LL_VER=Version (e.g. 1.1.1): "

if not defined LL_NUPKG (
    set /p "LL_NUPKG=nupkg path: "
    set "LL_NUPKG=!LL_NUPKG:"=!"
)

if not defined LL_SHARE set /p "LL_SHARE=Feijipan share URL: "

rem Release notes: single line, may be empty. Delayed expansion is disabled
rem here so that an exclamation mark in the notes survives.
if not defined LL_NOTES_SET (
    setlocal DisableDelayedExpansion
    set "LL_NOTES="
    set /p "LL_NOTES=Release notes (can be empty): "
    endlocal & set "LL_NOTES=%LL_NOTES%"
)

rem Admin API key: prefer the environment variable, otherwise prompt with
rem hidden input. --no-key-prompt keeps it strictly non-interactive.
set "LL_KEY=%LIVELENS_ADMIN_KEY%"
if not defined LL_KEY if not defined LL_NO_KEY_PROMPT (
    echo.
    echo [i] LIVELENS_ADMIN_KEY is not set; please type it ^(input is hidden^).
    for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "$s=Read-Host -AsSecureString 'Admin API Key'; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "LL_KEY=%%K"
)
if not defined LL_KEY (set "LL_ERR=3" & set "LL_ERRMSG=missing Admin API Key (LIVELENS_ADMIN_KEY is not set and no key was entered)" & goto :halted)

rem ---------------------------------------------------------------------
rem 2) PowerShell: validate every input, then compute SHA1 / SHA256 / size.
rem ---------------------------------------------------------------------
set "LL_RESULTFILE=%LL_TMP_RESULT%"
set "LL_JSONFILE=%LL_TMP_JSON%"

call :run_ps prepare
if errorlevel 1 (
    for /f "usebackq tokens=1,* delims==" %%A in ("%LL_TMP_RESULT%") do set "%%A=%%B"
    if not defined ERR set "ERR=5"
    rem NOTE: never write "endlocal & exit /b !ERR!". Delayed expansion is
    rem evaluated AFTER endlocal, so the code would be empty and the script
    rem would exit 0. Copy into LL_ERR (a normal variable) and let :halted do
    rem the endlocal with ordinary %..% expansion.
    set "LL_ERR=!ERR!"
    set "LL_ERRMSG=input validation failed (see the message above)"
    call :cleanup
    goto :halted
)

for /f "usebackq tokens=1,* delims==" %%A in ("%LL_TMP_RESULT%") do set "%%A=%%B"
if not defined SHA1 (set "LL_ERR=5" & set "LL_ERRMSG=could not read SHA1" & goto :halted)
if not defined SHA256 (set "LL_ERR=5" & set "LL_ERRMSG=could not read SHA256" & goto :halted)
if not defined SIZE (set "LL_ERR=5" & set "LL_ERRMSG=could not read file size" & goto :halted)

set "LL_NOTES_SHOW=%LL_NOTES%"
if defined LL_NOTES_FILE set "LL_NOTES_SHOW=(from file) %LL_NOTES_FILE%"

echo.
echo ----------------------------------------
echo File        : %LL_NAME%
echo Size        : %SIZE%
echo SHA1        : %SHA1%
echo SHA256      : %SHA256%
echo Version     : %LL_VER%
echo Share URL   : %LL_SHARE%
echo Notes       : %LL_NOTES_SHOW%
echo ----------------------------------------
echo.

set "LL_CONFIRM="
if not defined LL_YES set /p "LL_CONFIRM=Publish now? (Y/N): "
if defined LL_YES set "LL_CONFIRM=Y"
if /i not "!LL_CONFIRM!"=="Y" (set "LL_ERR=4" & set "LL_ERRMSG=cancelled by user, nothing was published" & goto :halted)

rem ---------------------------------------------------------------------
rem 3) Publish
rem ---------------------------------------------------------------------
call :run_ps publish
if errorlevel 1 (set "LL_ERR=6" & set "LL_ERRMSG=publish request failed (server rejected it, or the network is unreachable)" & goto :halted)

echo.
echo [OK] package exists
echo [OK] filename verified
echo [OK] SHA1 calculated
echo [OK] SHA256 calculated
echo [OK] size calculated
echo [OK] share URL validated
echo [OK] release published
echo.
echo Version       : %LL_VER%
echo Package       : %LL_NAME%
echo Size          : %SIZE%
echo Release notes : %LL_NOTES%

call :cleanup
endlocal & exit /b 0


rem =====================================================================
rem Subroutines
rem =====================================================================

:parse_args
if "%~1"=="" exit /b 0
if /i "%~1"=="--version" ( set "LL_VER=%~2" & shift & shift & goto :parse_args )
if /i "%~1"=="--nupkg"   ( set "LL_NUPKG=%~2" & shift & shift & goto :parse_args )
if /i "%~1"=="--share"   ( set "LL_SHARE=%~2" & shift & shift & goto :parse_args )
if /i "%~1"=="--notes"   ( set "LL_NOTES=%~2" & set "LL_NOTES_SET=1" & shift & shift & goto :parse_args )
if /i "%~1"=="--notes-file" ( set "LL_NOTES_FILE=%~2" & set "LL_NOTES_SET=1" & shift & shift & goto :parse_args )
if /i "%~1"=="--yes"     ( set "LL_YES=1" & shift & goto :parse_args )
if /i "%~1"=="--no-key-prompt" ( set "LL_NO_KEY_PROMPT=1" & shift & goto :parse_args )
echo [FAIL] unknown option: %~1
exit /b 9


:run_ps
rem %1 = prepare / publish
set "LL_MODE=%~1"
set "LL_PS="
set "LL_PS=%LL_PS%$ErrorActionPreference='Stop';"
set "LL_PS=%LL_PS%$rf=$env:LL_RESULTFILE;"
set "LL_PS=%LL_PS%try{"
set "LL_PS=%LL_PS%$ver=$env:LL_VER;$p=$env:LL_NUPKG;$share=$env:LL_SHARE;"
set "LL_PS=%LL_PS%if($env:LL_MODE -eq 'prepare'){"
set "LL_PS=%LL_PS%if($ver -notmatch '^\d+\.\d+\.\d+$'){Write-Host '[FAIL] version must be MAJOR.MINOR.PATCH';Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=1';exit 1};"
set "LL_PS=%LL_PS%if($p -eq ''){Write-Host '[FAIL] nupkg path is empty';Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=1';exit 1};"
set "LL_PS=%LL_PS%if(-not (Test-Path -LiteralPath $p -PathType Leaf)){Write-Host ('[FAIL] file not found: '+$p);Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=1';exit 1};"
set "LL_PS=%LL_PS%if(-not $p.ToLower().EndsWith('.nupkg')){Write-Host '[FAIL] file must end with .nupkg';Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=1';exit 1};"
set "LL_PS=%LL_PS%$nm=Split-Path -Leaf $p;$ex='LiveLensDesktop-'+$ver+'-stable-full.nupkg';"
set "LL_PS=%LL_PS%if($nm -ne $ex){Write-Host ('[FAIL] filename/version mismatch: expected '+$ex+' but got '+$nm);Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=1';exit 1};"
set "LL_PS=%LL_PS%if($share -notmatch '^https://(share\.feijipan\.com|www\.feijix\.com|www\.feijipan\.com)/s/.+'){Write-Host '[FAIL] only https://share.feijipan.com/s/... (or www.feijix.com / www.feijipan.com) is accepted';Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=2';exit 1};"
set "LL_PS=%LL_PS%$i=Get-Item -LiteralPath $p;$s1=(Get-FileHash -LiteralPath $p -Algorithm SHA1).Hash;$s256=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash;"
set "LL_PS=%LL_PS%Set-Content -LiteralPath $rf -Encoding ASCII -Value @('OK=1',('SHA1='+$s1),('SHA256='+$s256),('SIZE='+$i.Length),('NAME='+$i.Name));"
set "LL_PS=%LL_PS%exit 0};"
set "LL_PS=%LL_PS%$i=Get-Item -LiteralPath $p;$s1=(Get-FileHash -LiteralPath $p -Algorithm SHA1).Hash;$s256=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash;"
set "LL_PS=%LL_PS%$notes=$env:LL_NOTES;if($env:LL_NOTES_FILE){$notes=(Get-Content -LiteralPath $env:LL_NOTES_FILE -Raw -Encoding UTF8).TrimEnd()};"
set "LL_PS=%LL_PS%$o=[ordered]@{version=$ver;channel='stable';package_filename=$i.Name;package_share_url=$share;release_notes=$notes;package_sha1=$s1;package_sha256=$s256;package_size=[int64]$i.Length};"
set "LL_PS=%LL_PS%$j=$o|ConvertTo-Json -Depth 4 -Compress;"
set "LL_PS=%LL_PS%[System.IO.File]::WriteAllText($env:LL_JSONFILE,$j,(New-Object System.Text.UTF8Encoding($false)));"
set "LL_PS=%LL_PS%$r=Invoke-RestMethod -Uri $env:LL_API_URL -Method Post -Headers @{'X-API-Key'=$env:LL_KEY} -ContentType 'application/json; charset=utf-8' -InFile $env:LL_JSONFILE -TimeoutSec 90;"
set "LL_PS=%LL_PS%$r|ConvertTo-Json -Depth 4|Write-Host;"
set "LL_PS=%LL_PS%exit 0"
set "LL_PS=%LL_PS%}catch{Write-Host ('[FAIL] '+$_.Exception.Message);Set-Content -LiteralPath $rf -Encoding ASCII -Value 'ERR=6';exit 1}"

powershell -NoProfile -Command "%LL_PS%"
exit /b %errorlevel%


:halted
echo.
echo [FAIL] !LL_ERRMSG!
call :cleanup
endlocal & exit /b %LL_ERR%


:cleanup
if exist "%LL_TMP_JSON%" del /q "%LL_TMP_JSON%" >nul 2>&1
if exist "%LL_TMP_RESULT%" del /q "%LL_TMP_RESULT%" >nul 2>&1
exit /b 0
