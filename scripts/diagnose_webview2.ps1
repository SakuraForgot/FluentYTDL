<# Read-only diagnosis. Run in Windows PowerShell 5.1, in a separate process:
   powershell.exe -NoProfile -File .\diagnose_webview2.ps1 -AppDirectory 'E:\工具\FluentYTDL'
   Does not unblock files, change configuration or access browser profiles. #>
param([Parameter(Mandatory=$true)][string]$AppDirectory)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core') {
    throw 'Use Windows PowerShell (powershell.exe), not pwsh: this probe requires .NET Framework.'
}
$appRoot = (Resolve-Path -LiteralPath $AppDirectory).Path
$dllPath = Join-Path $appRoot '_internal\pythonnet\runtime\Python.Runtime.dll'
$result = [ordered]@{
    processBits = [IntPtr]::Size * 8
    clrVersion = [Environment]::Version.ToString()
    exists = (Test-Path -LiteralPath $dllPath -PathType Leaf)
}
try {
    $result.sha256 = (Get-FileHash -LiteralPath $dllPath -Algorithm SHA256).Hash
    $result.zoneIdentifier = $null -ne (Get-Item -LiteralPath $dllPath -Stream Zone.Identifier -ErrorAction SilentlyContinue)
    $assembly = [Reflection.Assembly]::LoadFrom($dllPath)
    $loaderType = $assembly.GetType('Python.Runtime.Loader', $true)
    $flags = [Reflection.BindingFlags]'Public,NonPublic,Static'
    $result.initializeFound = $null -ne $loaderType.GetMethod('Initialize', $flags)
    $result.assembly = $assembly.FullName
    $result.status = 'loaded (does not verify a full login)'
} catch {
    $errors = @()
    $currentException = $_.Exception
    while ($null -ne $currentException) {
        # Replace absolute app/user paths before sharing diagnostic output.
        $message = $currentException.Message.Replace($appRoot, '<app>')
        if ($env:USERPROFILE) { $message = $message.Replace($env:USERPROFILE, '<user>') }
        $errors += [ordered]@{ type = $currentException.GetType().FullName; hresult = $currentException.HResult; message = $message }
        $currentException = $currentException.InnerException
    }
    $result.status = 'failed'
    $result.errors = $errors
}
$result | ConvertTo-Json -Depth 6
