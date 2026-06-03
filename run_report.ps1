# 수동으로 오늘 리포트를 생성하고 GitHub에 푸시합니다.
Set-Location $PSScriptRoot

Write-Host "Generating daily market report..." -ForegroundColor Cyan
python generate_report.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Report generation failed!" -ForegroundColor Red
    exit 1
}

git add public/daily_market_report.json public/reports/
git diff --staged --quiet

if ($LASTEXITCODE -ne 0) {
    $date = Get-Date -Format "yyyy-MM-dd"
    git commit -m "chore: manual report $date"
    git push
    Write-Host "Done! Report pushed to GitHub." -ForegroundColor Green
} else {
    Write-Host "No changes to commit." -ForegroundColor Yellow
}
