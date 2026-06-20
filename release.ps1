# Script per automatizzare release con incremento versione
# Uso: .\release.ps1 "messaggio commit"

param(
    [string]$message = "fix: update per test"
)

$configPath = "histolite\config.yaml"

# Leggi versione corrente
$content = Get-Content $configPath -Raw
if ($content -match 'version:\s*"(\d+)\.(\d+)\.(\d+)"') {
    $major, $minor, $patch = $matches[1], $matches[2], $matches[3]
    $patch = [int]$patch + 1
    $newVersion = "$major.$minor.$patch"
    
    # Aggiorna config.yaml
    $newContent = $content -replace 'version:\s*".*?"', "version: `"$newVersion`""
    Set-Content $configPath $newContent
    
    Write-Host "✅ Versione aggiornata a $newVersion" -ForegroundColor Green
    
    # Commit e push
    git add $configPath
    git commit -m "$message - v$newVersion"
    git push
    
    Write-Host ""
    Write-Host "📋 Istruzioni per il Supervisor:" -ForegroundColor Cyan
    Write-Host "1. Store → ⋮ → Aggiorna repository"
    Write-Host "2. HistoLite → Aggiorna (non Rebuild)"
    Write-Host "3. Attendere il download della nuova immagine"
    Write-Host "4. Riavvia l'add-on"
    Write-Host ""
}
else {
    Write-Host "❌ Errore: versione non trovata in config.yaml" -ForegroundColor Red
}
