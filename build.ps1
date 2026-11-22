param(
    [string]$Python = "python",
    [string]$Destination = (Join-Path $PSScriptRoot "dist"),
    [string]$BuildDirectory = (Join-Path $PSScriptRoot "build")
)
$ErrorActionPreference = "Stop"
$projectPath = $PSScriptRoot
$encoder = & $Python -c "from pathlib import Path; import imageio_ffmpeg; print(next((Path(imageio_ffmpeg.__file__).parent / 'binaries').glob('ffmpeg-win-x86_64-*.exe')))"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $encoder -PathType Leaf)) {
    throw "Install the Windows x64 imageio-ffmpeg wheel listed in requirements-build.txt."
}
$argsList = @(
    "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx",
    "--name", "ClassicDrop", "--paths", (Join-Path $projectPath "vendor"),
    "--workpath", $BuildDirectory, "--specpath", $BuildDirectory, "--distpath", $Destination,
    "--icon", (Join-Path $projectPath "ClassicDrop.ico"),
    "--add-data", ((Join-Path $projectPath "ClassicDrop.ico") + ";."),
    "--add-data", ((Join-Path $projectPath "LICENSE") + ";licenses"),
    "--add-data", ((Join-Path $projectPath "vendor/COPYING.md") + ";licenses"),
    "--add-binary", ($encoder + ";imageio_ffmpeg/binaries"), "--hidden-import", "imageio_ffmpeg.binaries",
    "--collect-binaries", "wasmtime"
)
foreach ($dist in @("mutagen","imageio-ffmpeg","pycryptodome","pillow","wasmtime","whenever","tkinterdnd2","tzdata")) {
    $argsList += @("--copy-metadata", $dist)
}
$argsList += (Join-Path $projectPath "app.py")
& $Python @argsList
exit $LASTEXITCODE
