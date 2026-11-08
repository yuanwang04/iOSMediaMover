<#
.SYNOPSIS
    Dedicated build script that pulls/builds libimobiledevice from upstream GitHub
    and packages iOS Media Mover into a redistributable Windows release archive.

.DESCRIPTION
    - If `-BuildLibimobiledeviceFromSource` is set (or if pre-built binaries are not found
      locally), uses MSYS2 (MINGW64) to clone/pull and compile the upstream libimobiledevice
      repositories (`libplist`, `libimobiledevice-glue`, `libusbmuxd`, `libtatsu`,
      `libimobiledevice`) from https://github.com/libimobiledevice.
    - Collects the required shared libraries (`.dll`) and tools (`afcclient.exe`,
      `idevice_id.exe`, etc.) into `bin/`. Keeping the `.dll` files external in `bin/`
      satisfies LGPL v2.1 dynamic linking requirements.
    - Builds `iOSMediaMover.exe` (via PyInstaller `--onedir` or native Windows launcher)
      and packages `dist/iOSMediaMover-Windows-x64.zip` along with `LICENSE` and
      `THIRD_PARTY_NOTICES.md`.
#>

param(
    [switch]$BuildLibimobiledeviceFromSource,
    [string]$Msys2Root = "C:\msys64",
    [string]$LocalLibimobiledeviceBin = "C:\Program Files\libimobiledevice\mingw64\bin"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$BinDir = Join-Path $RepoRoot "bin"
$BuildDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"
$StagingDir = Join-Path $DistDir "iOSMediaMover"

$RequiredFiles = @(
    "afcclient.exe",
    "idevice_id.exe",
    "idevicename.exe",
    "ideviceinfo.exe",
    "idevicepair.exe",
    "libimobiledevice-1.0.dll",
    "libimobiledevice-glue-1.0.dll",
    "libplist-2.0.dll",
    "libusbmuxd-2.0.dll",
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll"
)

function Build-LibimobiledeviceWithMsys2 {
    $BashExe = Join-Path $Msys2Root "usr\bin\bash.exe"
    if (-not (Test-Path $BashExe)) {
        throw "MSYS2 bash not found at '$BashExe'. Install MSYS2 or pass -Msys2Root."
    }

    $SrcRoot = Join-Path $BuildDir "libimobiledevice-src"
    New-Item -ItemType Directory -Force -Path $SrcRoot | Out-Null
    $InstallPrefix = Join-Path $BuildDir "libimobiledevice-prefix"
    New-Item -ItemType Directory -Force -Path $InstallPrefix | Out-Null

    $MsysSrcRoot = ($SrcRoot -replace '\\', '/') -replace '^([A-Za-z]):', '/$1'
    $MsysPrefix = ($InstallPrefix -replace '\\', '/') -replace '^([A-Za-z]):', '/$1'

    $BashScript = @"
set -euo pipefail
pacman -S --needed --noconfirm \
    base-devel git make libtool autoconf automake-wrapper \
    mingw-w64-x86_64-gcc mingw-w64-x86_64-pkg-config mingw-w64-x86_64-openssl

export PATH="/mingw64/bin:`$PATH"
export PKG_CONFIG_PATH="$MsysPrefix/lib/pkgconfig:/mingw64/lib/pkgconfig"
export CPPFLAGS="-I$MsysPrefix/include"
export LDFLAGS="-L$MsysPrefix/lib"

cd "$MsysSrcRoot"

repos=(
    "libplist"
    "libimobiledevice-glue"
    "libusbmuxd"
    "libtatsu"
    "libimobiledevice"
)

for repo in "`${repos[@]}"; do
    if [ -d "`$repo/.git" ]; then
        echo "--> Pulling latest `$repo..."
        git -C "`$repo" pull --ff-only
    else
        echo "--> Cloning `$repo..."
        git clone --depth 1 "https://github.com/libimobiledevice/`$repo.git" "`$repo"
    fi
    pushd "`$repo"
    ./autogen.sh --prefix="$MsysPrefix" --without-cython
    make -j"`$(nproc)"
    make install
    popd
done
"@

    $Env:MSYSTEM = "MINGW64"
    $Env:CHERE_INVOKING = "1"
    & $BashExe -lc $BashScript

    $BuiltBin = Join-Path $InstallPrefix "bin"
    $MingwBin = Join-Path $Msys2Root "mingw64\bin"

    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    foreach ($f in $RequiredFiles) {
        $cand1 = Join-Path $BuiltBin $f
        $cand2 = Join-Path $MingwBin $f
        if (Test-Path $cand1) {
            Copy-Item $cand1 -Destination (Join-Path $BinDir $f) -Force
        } elseif (Test-Path $cand2) {
            Copy-Item $cand2 -Destination (Join-Path $BinDir $f) -Force
        } else {
            throw "Required runtime binary '$f' was not found after building libimobiledevice."
        }
    }
}

# Step 1: Populate bin/ (either by pulling/building from GitHub via MSYS2 or copying from local build)
$NeedBuild = $BuildLibimobiledeviceFromSource.IsPresent
if (-not $NeedBuild) {
    $AllInBin = $true
    foreach ($f in $RequiredFiles) {
        if (-not (Test-Path (Join-Path $BinDir $f))) {
            $AllInBin = $false
            break
        }
    }
    if (-not $AllInBin) {
        if (Test-Path $LocalLibimobiledeviceBin) {
            Write-Host "--> Copying prebuilt libimobiledevice binaries from '$LocalLibimobiledeviceBin'..."
            New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
            foreach ($f in $RequiredFiles) {
                Copy-Item (Join-Path $LocalLibimobiledeviceBin $f) -Destination (Join-Path $BinDir $f) -Force
            }
        } else {
            $NeedBuild = $true
        }
    }
}

if ($NeedBuild) {
    Write-Host "--> Pulling and building libimobiledevice from upstream GitHub repositories..."
    Build-LibimobiledeviceWithMsys2
}

# Step 2: Prepare Staging Directory
if (Test-Path $StagingDir) {
    Remove-Item -Recurse -Force $StagingDir
}
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StagingDir "bin") | Out-Null

# Step 3: Build Standalone Executable if PyInstaller is installed, otherwise compile lightweight launcher
$PyInstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if ($PyInstaller) {
    Write-Host "--> Building standalone executable with PyInstaller..."
    & pyinstaller --noconfirm --clean --windowed --onedir `
        --name "iOSMediaMover" `
        --distpath $DistDir `
        --workpath (Join-Path $BuildDir "pyinstaller") `
        --specpath $BuildDir `
        (Join-Path $RepoRoot "move_ios_media.py")
    New-Item -ItemType Directory -Force -Path (Join-Path $StagingDir "bin") | Out-Null
} else {
    $CscPath = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (Test-Path $CscPath) {
        Write-Host "--> Compiling Windows GUI launcher (iOSMediaMover.exe)..."
        $LauncherExe = Join-Path $StagingDir "iOSMediaMover.exe"
        $CsSource = @"
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

class Program {
    [STAThread]
    static int Main(string[] args) {
        string appDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = Path.Combine(appDir, "move_ios_media.py");
        if (!File.Exists(scriptPath)) {
            MessageBox.Show("Could not find move_ios_media.py in:\n" + appDir, "iOS Media Mover", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string binDir = Path.Combine(appDir, "bin");
        Environment.SetEnvironmentVariable("LIBIMOBILEDEVICE_PATH", binDir);
        string currentPath = Environment.GetEnvironmentVariable("PATH") ?? "";
        Environment.SetEnvironmentVariable("PATH", binDir + ";" + currentPath);

        string quotedScript = "\"" + scriptPath + "\"";
        string joinedArgs = args.Length > 0 ? " " + string.Join(" ", args) : "";
        bool isCli = Array.IndexOf(args, "--cli") >= 0 || Array.IndexOf(args, "-h") >= 0 || Array.IndexOf(args, "--help") >= 0;

        string[] candidates = isCli
            ? new string[] { "python.exe" }
            : new string[] { "pythonw.exe", "python.exe" };

        foreach (string candidate in candidates) {
            try {
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = candidate;
                psi.Arguments = quotedScript + joinedArgs;
                psi.WorkingDirectory = appDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = !isCli;
                Process p = Process.Start(psi);
                if (p != null) {
                    if (isCli) p.WaitForExit();
                    return 0;
                }
            } catch {}
        }

        MessageBox.Show(
            "Python 3.9+ was not found on PATH.\nPlease install Python 3 from https://www.python.org/ or the Microsoft Store.",
            "iOS Media Mover",
            MessageBoxButtons.OK,
            MessageBoxIcon.Warning
        );
        return 1;
    }
}
"@
        New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
        $TempCs = Join-Path $BuildDir "Launcher.cs"
        Set-Content -Path $TempCs -Value $CsSource -Encoding UTF8
        & $CscPath /nologo /target:winexe /out:$LauncherExe /reference:System.Windows.Forms.dll $TempCs
        Remove-Item -Force $TempCs
    }
}

# Step 4: Copy source, dynamic libraries (bin/), and required LGPL v2.1 / third-party license notices
Copy-Item (Join-Path $RepoRoot "move_ios_media.py") -Destination $StagingDir -Force
Copy-Item (Join-Path $RepoRoot "README.md") -Destination $StagingDir -Force
Copy-Item (Join-Path $RepoRoot "LICENSE") -Destination $StagingDir -Force
Copy-Item (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md") -Destination $StagingDir -Force
Copy-Item (Join-Path $BinDir "*") -Destination (Join-Path $StagingDir "bin") -Recurse -Force

$ZipPath = Join-Path $DistDir "iOSMediaMover-Windows-x64.zip"
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}
Compress-Archive -Path $StagingDir -DestinationPath $ZipPath
Write-Host "--> Successfully created release package: $ZipPath"
