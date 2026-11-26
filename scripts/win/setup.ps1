# Windows 测试机一键安装（PowerShell，在仓库根目录运行）
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "安装 uv ..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv sync --extra windows
New-Item -ItemType Directory -Force -Path D:\cadmcp\workspaces, D:\cadmcp\releases, D:\cadmcp\library\solidworks | Out-Null
if (-not (Test-Path D:\cadmcp\node.yaml)) { Copy-Item config\node.windows.example.yaml D:\cadmcp\node.yaml }
if (-not (Test-Path D:\cadmcp\policy.yaml)) { Copy-Item config\policy.example.yaml D:\cadmcp\policy.yaml }
uv run pytest -q
uv run cadmcp doctor --node-config D:\cadmcp\node.yaml
