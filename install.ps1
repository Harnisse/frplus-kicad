# frplus のインストール。KiCad だけが入っている Windows で動く (管理者権限は不要)。
#
#   install.bat をダブルクリックするか、PowerShell で:
#     powershell -ExecutionPolicy Bypass -File install.ps1 [-Dest フォルダ] [-PluginDest フォルダ] [-ForceJre] [-NoPlugin]
#     powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#
# やること:
#   1. KiCad を探す (付属の python を使うので、別途 Python は要らない)
#   2. ツール本体を %LOCALAPPDATA%\frplus にコピーする
#   3. Java 25 以降が無ければ、Eclipse Temurin の JRE を取得して frplus\jre に展開する (約 60 MB、PATH は変えない)
#   4. Freerouting の jar が同梱されていなければ、公式リリースを取得する
#   5. KiCad のツールバーにボタンを足す
param(
    [string]$Dest = (Join-Path $env:LOCALAPPDATA 'frplus'),
    [string]$PluginDest = '',
    [switch]$ForceJre,
    [switch]$NoPlugin,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # 進捗バーを出すと Invoke-WebRequest が極端に遅くなる
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$MinJava = 25
$JreApi = "https://api.adoptium.net/v3/assets/latest/$MinJava/hotspot?os=windows&architecture=x64&image_type=jre"
$JarUrl = 'https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar'

function Say($text) { Write-Host $text }
function Fail($text) { Write-Host "エラー: $text" -ForegroundColor Red; exit 1 }

function Find-KiCad {
    $dirs = Get-ChildItem 'C:\Program Files\KiCad' -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    foreach ($d in $dirs) {
        $bin = Join-Path $d.FullName 'bin'
        if ((Test-Path (Join-Path $bin 'python.exe')) -and (Test-Path (Join-Path $bin 'kicad-cli.exe'))) { return $bin }
    }
    return $null
}

function Get-JavaMajor($exe) {
    # java -version は標準エラーに書く。PowerShell 5.1 では 2>&1 がエラー扱いになるので cmd 経由で読む
    try { $out = & cmd /c "`"$exe`" -version 2>&1" } catch { return 0 }
    if ("$out" -match 'version "(\d+)') { return [int]$Matches[1] }
    return 0
}

function Get-PluginDir($kicadBin) {
    if ($PluginDest) { return $PluginDest }
    $ver = Split-Path -Leaf (Split-Path -Parent $kicadBin)
    return (Join-Path ([Environment]::GetFolderPath('MyDocuments')) "KiCad\$ver\scripting\plugins")
}

$kicad = Find-KiCad

if ($Uninstall) {
    if ($kicad) {
        $loader = Join-Path (Get-PluginDir $kicad) 'frplus_plugin.py'
        if (Test-Path $loader) { Remove-Item $loader -Confirm:$false; Say "削除: $loader" }
    }
    # 中身が frplus であることを確かめてから消す
    if ((Test-Path (Join-Path $Dest 'frplus\route.py'))) {
        Remove-Item $Dest -Recurse -Force -Confirm:$false
        Say "削除: $Dest"
    } else {
        Say "frplus は見つかりませんでした: $Dest"
    }
    exit 0
}

if (-not $kicad) { Fail 'KiCad が見つかりません (C:\Program Files\KiCad)。先に KiCad 9 をインストールしてください。' }
Say "KiCad: $kicad"
if (-not (Test-Path (Join-Path $Src 'frplus\route.py'))) { Fail "このスクリプトと同じフォルダに frplus フォルダがありません: $Src" }

# --- 2. 本体のコピー ---
New-Item -ItemType Directory -Force $Dest | Out-Null
if ((Resolve-Path $Src).Path.TrimEnd('\') -ne (Resolve-Path $Dest).Path.TrimEnd('\')) {
    $target = Join-Path $Dest 'frplus'
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -Confirm:$false }
    Copy-Item (Join-Path $Src 'frplus') $target -Recurse
    Get-ChildItem $target -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force -Confirm:$false
    foreach ($f in 'README.md', 'NOTICE.txt', 'install.ps1', 'install.bat') {
        if (Test-Path (Join-Path $Src $f)) { Copy-Item (Join-Path $Src $f) $Dest -Force }
    }
    if (Test-Path (Join-Path $Src 'freerouting')) {
        Get-ChildItem (Join-Path $Src 'freerouting') -Recurse -Filter '*.jar' | ForEach-Object {
            $rel = $_.FullName.Substring((Join-Path $Src 'freerouting').Length + 1)
            $to = Join-Path (Join-Path $Dest 'freerouting') $rel
            New-Item -ItemType Directory -Force (Split-Path -Parent $to) | Out-Null
            Copy-Item $_.FullName $to -Force
        }
    }
    Say "コピー: $Dest"
}

# --- 3. Java ---
$bundled = Join-Path $Dest 'jre\bin\java.exe'
$java = $null
if (-not $ForceJre) {
    if ((Test-Path $bundled) -and (Get-JavaMajor $bundled) -ge $MinJava) { $java = $bundled }
    elseif ((Get-Command java -ErrorAction SilentlyContinue) -and (Get-JavaMajor 'java') -ge $MinJava) { $java = 'java (PATH)' }
}
if ($java) {
    Say "Java: $java"
} else {
    Say "Java $MinJava 以降が無いので、Eclipse Temurin の JRE を取得します (約 60 MB)..."
    $asset = (Invoke-RestMethod $JreApi)[0].binary.package
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest $asset.link -OutFile $zip -UseBasicParsing
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $asset.checksum.ToLower()) { Remove-Item $zip -Confirm:$false; Fail 'JRE のチェックサムが一致しません。もう一度実行してください。' }
    $tmp = Join-Path $Dest 'jre_tmp'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -Confirm:$false }
    Expand-Archive $zip $tmp
    $jre = Join-Path $Dest 'jre'
    if (Test-Path $jre) { Remove-Item $jre -Recurse -Force -Confirm:$false }
    Move-Item (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName $jre      # zip の中は jdk-25.x-jre\ の 1 階層
    Remove-Item $tmp -Recurse -Force -Confirm:$false
    Remove-Item $zip -Confirm:$false
    if ((Get-JavaMajor $bundled) -lt $MinJava) { Fail "展開した JRE を起動できません: $bundled" }
    Say "Java: $bundled"
}

# --- 4. Freerouting ---
$jar = Get-ChildItem (Join-Path $Dest 'freerouting') -Recurse -Filter '*.jar' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jar) {
    Say 'Freerouting の jar が同梱されていないので、公式リリース v2.5.0 を取得します (約 65 MB)...'
    New-Item -ItemType Directory -Force (Join-Path $Dest 'freerouting') | Out-Null
    Invoke-WebRequest $JarUrl -OutFile (Join-Path $Dest 'freerouting\freerouting-2.5.0.jar') -UseBasicParsing
}

# --- 5. ボタンと確認 ---
$py = Join-Path $kicad 'python.exe'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Push-Location $Dest
try {
    & $py -m frplus doctor
    if ($LASTEXITCODE -ne 0) { Fail '必要なものが揃っていません (上の表示を確認してください)。' }
    if (-not $NoPlugin) {
        & $py -m frplus install-plugin --dest (Get-PluginDir $kicad)
        if ($LASTEXITCODE -ne 0) { Fail 'KiCad のボタンを登録できませんでした。' }
    }
} finally {
    Pop-Location
}
Say ''
Say 'インストールが終わりました。KiCad を起動し直すと、基板エディターのツールバーに frplus のボタンが出ます。'
Say "コマンドから使う場合:  cd `"$Dest`"  してから  `"$py`" -m frplus route <基板.kicad_pcb>"
