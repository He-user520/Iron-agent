#!/usr/bin/env pwsh
# Iron CLI 并行开发 worktree 隔离脚本
# 用法：
#   .\setup-worktrees.ps1              # 创建所有 worktree
#   .\setup-worktrees.ps1 -Clean       # 清理所有 worktree
#   .\setup-worktrees.ps1 -Track 1     # 仅创建 Track 1

param(
    [switch]$Clean,
    [int]$Track = 0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = "d:\嵌入式-Agent"
$WorktreeBase = "d:\嵌入式-Agent-worktrees"

# Track 定义
$Tracks = @(
    @{ Id = 1; Name = "engine-split";     Branch = "feature/track-1-engine-split";     Plan = "docs/plans/track-1-engine-split.md" },
    @{ Id = 2; Name = "main-split";       Branch = "feature/track-2-main-split";       Plan = "docs/plans/track-2-main-split.md" },
    @{ Id = 3; Name = "stream-recovery";  Branch = "feature/track-3-stream-recovery";  Plan = "docs/plans/track-3-stream-recovery.md" }
)

function Invoke-Git {
    param([string]$Cwd, [string]$Command)
    Push-Location $Cwd
    try {
        Invoke-Expression "git $Command"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "git $Command failed with exit code $LASTEXITCODE"
            exit 1
        }
    }
    finally {
        Pop-Location
    }
}

function New-Worktree {
    param([hashtable]$TrackInfo)
    $wtPath = Join-Path $WorktreeBase "track-$($TrackInfo.Id)-$($TrackInfo.Name)"
    $branch = $TrackInfo.Branch

    Write-Host "[$($TrackInfo.Id)] Creating worktree: $wtPath" -ForegroundColor Cyan

    # 检查 worktree 是否已存在
    if (Test-Path $wtPath) {
        Write-Host "  [SKIP] Worktree already exists: $wtPath" -ForegroundColor Yellow
        return
    }

    # 创建工作目录
    if (-not (Test-Path $WorktreeBase)) {
        New-Item -ItemType Directory -Path $WorktreeBase -Force | Out-Null
        Write-Host "  [OK] Created worktree base: $WorktreeBase" -ForegroundColor Green
    }

    # 创建分支（如果不存在）并添加 worktree
    Invoke-Git -Cwd $ProjectRoot -Command "worktree add -b $branch `"$wtPath`""

    Write-Host "  [OK] Worktree created: $wtPath" -ForegroundColor Green
    Write-Host "  [OK] Branch: $branch" -ForegroundColor Green

    # 复制子计划文档到 worktree（便于参考）
    $planSrc = Join-Path $ProjectRoot $TrackInfo.Plan
    $planDir = Join-Path $wtPath "docs/plans"
    if (Test-Path $planSrc) {
        if (-not (Test-Path $planDir)) {
            New-Item -ItemType Directory -Path $planDir -Force | Out-Null
        }
        Copy-Item $planSrc -Destination $planDir -Force
        Write-Host "  [OK] Plan copied: $($TrackInfo.Plan)" -ForegroundColor Green
    }

    # 创建 venv（如不存在）
    $venvPath = Join-Path $wtPath ".venv"
    if (-not (Test-Path $venvPath)) {
        Write-Host "  [INFO] Creating venv (this may take a moment)..." -ForegroundColor Cyan
        Push-Location $wtPath
        try {
            python -m venv .venv
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  [WARN] venv creation failed, skipping" -ForegroundColor Yellow
            } else {
                & "$venvPath\Scripts\pip.exe" install -e ".[dev]" 2>$null | Out-Null
                Write-Host "  [OK] venv created and package installed" -ForegroundColor Green
            }
        }
        finally {
            Pop-Location
        }
    }

    Write-Host ""
    Write-Host "  Track $($TrackInfo.Id) ready:" -ForegroundColor Green
    Write-Host "    Path:   $wtPath" -ForegroundColor White
    Write-Host "    Branch: $branch" -ForegroundColor White
    Write-Host "    Plan:   $($TrackInfo.Plan)" -ForegroundColor White
    Write-Host "    Enter:  cd `"$wtPath`"" -ForegroundColor White
    Write-Host ""
}

function Remove-AllWorktrees {
    Write-Host "Cleaning up all worktrees..." -ForegroundColor Yellow

    foreach ($t in $Tracks) {
        $wtPath = Join-Path $WorktreeBase "track-$($t.Id)-$($t.Name)"
        if (Test-Path $wtPath) {
            Write-Host "[$($t.Id)] Removing worktree: $wtPath" -ForegroundColor Cyan
            Invoke-Git -Cwd $ProjectRoot -Command "worktree remove --force `"$wtPath`""
        }
    }

    # 删除分支（可选，保留以防需要）
    foreach ($t in $Tracks) {
        Write-Host "[$($t.Id)] Deleting branch: $($t.Branch)" -ForegroundColor Cyan
        Invoke-Git -Cwd $ProjectRoot -Command "branch -D $($t.Branch)" 2>$null
    }

    # 删除基础目录
    if (Test-Path $WorktreeBase) {
        Remove-Item $WorktreeBase -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] Worktree base removed: $WorktreeBase" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "Cleanup complete." -ForegroundColor Green
}

function Show-Status {
    Write-Host ""
    Write-Host "=== Iron CLI Worktree Status ===" -ForegroundColor Cyan
    Write-Host ""

    Invoke-Git -Cwd $ProjectRoot -Command "worktree list"

    Write-Host ""
    Write-Host "=== Tracks ===" -ForegroundColor Cyan
    foreach ($t in $Tracks) {
        $wtPath = Join-Path $WorktreeBase "track-$($t.Id)-$($t.Name)"
        $exists = Test-Path $wtPath
        $status = if ($exists) { "READY" } else { "NOT CREATED" }
        $color = if ($exists) { "Green" } else { "Gray" }

        Write-Host "  [$($t.Id)] $($t.Name.PadRight(20)) $status" -ForegroundColor $color
        if ($exists) {
            Write-Host "      Path:   $wtPath" -ForegroundColor White
            Write-Host "      Branch: $($t.Branch)" -ForegroundColor White
            Write-Host "      Plan:   $($t.Plan)" -ForegroundColor White
        }
    }
    Write-Host ""
}

# === 主逻辑 ===

if ($Clean) {
    Remove-AllWorktrees
    exit 0
}

# 确保在项目根目录且 git 仓库干净
Push-Location $ProjectRoot
$status = git status --porcelain
$branch = git branch --show-current
Pop-Location

Write-Host "Current branch: $branch" -ForegroundColor Cyan
if ($status) {
    Write-Host "[WARN] Working tree has uncommitted changes:" -ForegroundColor Yellow
    Write-Host $status
    $proceed = Read-Host "Continue anyway? (y/N)"
    if ($proceed -ne "y") {
        Write-Host "Aborted." -ForegroundColor Red
        exit 1
    }
}

# 创建 worktree
if ($Track -gt 0) {
    $target = $Tracks | Where-Object { $_.Id -eq $Track }
    if ($target) {
        New-Worktree -TrackInfo $target
    } else {
        Write-Error "Invalid Track number: $Track"
        exit 1
    }
} else {
    foreach ($t in $Tracks) {
        New-Worktree -TrackInfo $t
    }
}

Show-Status

Write-Host "=== Parallel Execution Guide ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Track 1 (engine-split)    : Fully independent, no blockers" -ForegroundColor Green
Write-Host "Track 2 (main-split)      : Fully independent, no blockers" -ForegroundColor Green
Write-Host "Track 3 (stream-recovery) : Step 1-3 parallel, Step 4-6 needs Track 1" -ForegroundColor Yellow
Write-Host ""
Write-Host "=== Merge Order ===" -ForegroundColor Cyan
Write-Host "1. Track 1 first (engine.py refactor is the bottleneck)" -ForegroundColor White
Write-Host "2. Track 2 anytime (no conflicts)" -ForegroundColor White
Write-Host "3. Track 3 after Track 1 merged (rebase engine.py changes)" -ForegroundColor White
Write-Host ""
Write-Host "=== Verify After Each Merge ===" -ForegroundColor Cyan
Write-Host "  pytest tests/ -v  # must be >= 738 passed" -ForegroundColor White
Write-Host ""
