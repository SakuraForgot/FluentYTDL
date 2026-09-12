param(
    [Parameter(Mandatory=$true)][ValidateSet('Register','Stop','Clean','AddPath','RemovePath')][string]$Action,
    [Parameter(Mandatory=$true)][string]$AppDirectory,
    [ValidateSet('user','machine')][string]$Scope = 'user'
)
$ErrorActionPreference = 'Stop'
$appRoot = [IO.Path]::GetFullPath($AppDirectory).TrimEnd('\')
if ($appRoot -eq [IO.Path]::GetPathRoot($appRoot).TrimEnd('\')) { throw 'Refusing volume root' }
$failures = [Collections.Generic.List[string]]::new()

function Assert-NoReparse([string]$Path) {
    $itemPath = [IO.Path]::GetFullPath($Path)
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -LiteralPath $itemPath -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $itemPath" }
        }
        $parent = [IO.Directory]::GetParent($itemPath)
        if ($null -eq $parent) { break }
        $itemPath = $parent.FullName
    }
}
Assert-NoReparse $appRoot

if ($Action -eq 'Register') {
    $ownerFile = Join-Path $appRoot '.install-owner.json'
    if (-not (Test-Path -LiteralPath $ownerFile)) {
        @{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
          profile=[Environment]::GetFolderPath('UserProfile'); scope=$Scope} |
            ConvertTo-Json | Set-Content -LiteralPath $ownerFile -Encoding UTF8
    }
    exit 0
}

if ($Action -eq 'Stop') {
    # Verify paths, never kill another portable installation by its executable name.
    $processes = @(Get-CimInstance Win32_Process)
    if ($processes | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -ieq "$appRoot\updater.exe" }) {
        throw 'An update is in progress. Finish the update before installing or uninstalling.'
    }
    $owned = @($processes | Where-Object {
        $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq "$appRoot\FluentYTDL.exe")
    })
    $ids = [Collections.Generic.HashSet[int]]::new()
    foreach ($process in $owned) { [void]$ids.Add([int]$process.ProcessId) }
    do {
        $changed = $false
        foreach ($process in $processes) {
            if ($ids.Contains([int]$process.ParentProcessId) -and $ids.Add([int]$process.ProcessId)) { $changed = $true }
        }
    } while ($changed)
    foreach ($process in $owned) {
        $running = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        if ($running) { [void]$running.CloseMainWindow() }
    }
    if ($owned.Count) { Start-Sleep -Seconds 5 }
    foreach ($process in $processes | Where-Object { $ids.Contains([int]$_.ProcessId) }) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
        if ($current -and $current.CreationDate -eq $process.CreationDate) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        }
    }
    exit 0
}

if ($Action -in @('AddPath','RemovePath')) {
    $base = if ($Scope -eq 'machine') { [Microsoft.Win32.Registry]::LocalMachine } else {
        $ownerFile = Join-Path $appRoot '.install-owner.json'
        Assert-NoReparse $ownerFile
        $owner = Get-Content -LiteralPath $ownerFile -Raw | ConvertFrom-Json
        $userHive = [Microsoft.Win32.Registry]::Users.OpenSubKey($owner.sid, $true)
        if ($null -eq $userHive) { throw 'Original user registry hive is unavailable; run uninstall as that user' }
        $userHive
    }
    $environmentKey = if ($Scope -eq 'machine') { 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment' } else { 'Environment' }
    $key = $base.CreateSubKey($environmentKey)
    $record = $base.CreateSubKey('Software\FluentYTDL\Installer')
    $recordName = $appRoot.ToLowerInvariant()
    $original = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $kind = if ($key.GetValueNames() -contains 'Path') { $key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
    $parts = [Collections.Generic.List[string]]::new()
    foreach ($part in $original.Split(';')) { $parts.Add($part) }
    $added = @($record.GetValue($recordName, @()))
    if ($Action -eq 'AddPath') {
        foreach ($subdir in @('yt-dlp','ffmpeg','deno','atomicparsley','pot-provider')) {
            $candidate = "$appRoot\bin\$subdir"
            if (-not ($parts | Where-Object { $_.Trim().TrimEnd('\') -ieq $candidate })) {
                $parts.Add($candidate)
                $added += $candidate
            }
        }
        if ($added.Count) { $record.SetValue($recordName, [string[]]$added, [Microsoft.Win32.RegistryValueKind]::MultiString) }
    } else {
        foreach ($candidate in $added) {
            for ($index = $parts.Count - 1; $index -ge 0; $index--) {
                if ($parts[$index].Trim().TrimEnd('\') -ieq $candidate) { $parts.RemoveAt($index) }
            }
        }
        $record.DeleteValue($recordName, $false)
    }
    $key.SetValue('Path', ($parts -join ';'), $kind)
    $key.Dispose(); $record.Dispose()
    if ($Scope -eq 'user') { $base.Dispose() }
    exit 0
}

# Collect known download destinations before deleting any configuration.
$windowsProfiles = @(Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.LocalPath })
$profiles = if ($Scope -eq 'machine') {
    @($windowsProfiles | ForEach-Object { $_.LocalPath })
} else {
    $ownerFile = Join-Path $appRoot '.install-owner.json'
    if (-not (Test-Path -LiteralPath $ownerFile)) { throw 'Installation owner record missing; cannot safely identify user data' }
    Assert-NoReparse $ownerFile
    $owner = Get-Content -LiteralPath $ownerFile -Raw | ConvertFrom-Json
    $originalProfile = @($windowsProfiles | Where-Object { $_.SID -eq $owner.sid })
    if ($originalProfile.Count -ne 1) { throw 'Original installation user profile cannot be resolved' }
    @($originalProfile[0].LocalPath)
}
$roots = [Collections.Generic.List[string]]::new()
$registrations = [Collections.Generic.List[string]]::new()
$roots.Add($appRoot)
foreach ($profilePath in $profiles) {
    foreach ($relative in @('AppData\Local\FluentYTDL','AppData\Roaming\FluentYTDL','Documents\FluentYTDL')) {
        $roots.Add((Join-Path $profilePath $relative))
    }
    $profileRecord = $windowsProfiles | Where-Object { $_.LocalPath -ieq $profilePath } | Select-Object -First 1
    $knownFolders = [Microsoft.Win32.Registry]::Users.OpenSubKey("$($profileRecord.SID)\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
    if ($knownFolders) {
        foreach ($folder in @('Local AppData','AppData','Personal')) {
            $location = [string]$knownFolders.GetValue($folder, '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $location = $location.Replace('%USERPROFILE%', $profilePath)
            if ($location -and -not $location.Contains('%') -and [IO.Path]::IsPathRooted($location)) {
                $roots.Add((Join-Path $location 'FluentYTDL'))
            }
        }
        $knownFolders.Dispose()
    }
    $registryDir = Join-Path $profilePath 'AppData\Local\FluentYTDL\installations'
    if (Test-Path -LiteralPath $registryDir) {
        Assert-NoReparse $registryDir
        foreach ($registration in Get-ChildItem -LiteralPath $registryDir -File -Filter '*.json') {
            $record = Get-Content -LiteralPath $registration.FullName -Raw | ConvertFrom-Json
            if ($record.app_dir -ieq $appRoot -and $record.data_dir) {
                $registrations.Add($registration.FullName)
                $customRoot = [IO.Path]::GetFullPath($record.data_dir)
                $marker = Join-Path $customRoot '.fluentytdl-data-owner.json'
                if (Test-Path -LiteralPath $marker) {
                    Assert-NoReparse $marker
                    $owner = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
                    if ($owner.app_dir -ieq $appRoot) { $roots.Add($customRoot) }
                }
            }
        }
    }
}
# Shared registration keeps custom data roots discoverable even when a user's hive is offline.
$sharedRecords = Join-Path $appRoot 'bin\installations'
if (Test-Path -LiteralPath $sharedRecords) {
    Assert-NoReparse $sharedRecords
    foreach ($registration in Get-ChildItem -LiteralPath $sharedRecords -File -Filter '*.json') {
        Assert-NoReparse $registration.FullName
        $record = Get-Content -LiteralPath $registration.FullName -Raw | ConvertFrom-Json
        if ($record.app_dir -ieq $appRoot -and $record.data_dir) {
            $marker = Join-Path $record.data_dir '.fluentytdl-data-owner.json'
            if (Test-Path -LiteralPath $marker) {
                Assert-NoReparse $marker
                $owner = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
                if ($owner.app_dir -ieq $appRoot) { $roots.Add([IO.Path]::GetFullPath($record.data_dir)) }
            }
            $registrations.Add($registration.FullName)
        }
    }
}
$protected = [Collections.Generic.List[string]]::new()
foreach ($root in $roots) {
    $config = Join-Path $root 'config.json'
    if (Test-Path -LiteralPath $config) {
        Assert-NoReparse $config
        try { $settings = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json }
        catch { $failures.Add("Cannot read download destinations: $config"); continue }
        foreach ($name in @('download_dir','quick_download_dir')) {
            if ($settings.$name) { $protected.Add([IO.Path]::GetFullPath($settings.$name).TrimEnd('\')) }
        }
    }
}
$media = @('.mp4','.mkv','.webm','.avi','.mov','.mp3','.m4a','.flac','.wav','.ogg','.opus','.srt','.ass','.ssa','.vtt','.lrc')
function Remove-OwnedTree([string]$Path, [bool]$PreserveMedia = $true) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        Assert-NoReparse $Path
        $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        foreach ($downloadRoot in $protected) {
            if ($full -ieq $downloadRoot) { return }
        }
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $Path -Force) { Remove-OwnedTree $child.FullName $PreserveMedia }
            if (-not (Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $Path -Force }
        } elseif (-not $PreserveMedia -or $item.Extension.ToLowerInvariant() -notin $media) {
            Remove-Item -LiteralPath $Path -Force
        }
    } catch { $failures.Add("$Path : $_") }
}
foreach ($root in $roots | Select-Object -Unique) {
    foreach ($name in @('config.json','tasks.db','tasks.db-wal','tasks.db-shm','state','logs','cache','data',
        '.webview_profile','.fluent_temp','_update_tmp','_internal.old','FluentYTDL.exe.old','updater.exe.new',
        'update_manifest_cache.json','error_rules.override.json','.migrate_tmp','.migrated_v2','.migrated_to.txt',
        'legacy_conflict_install','legacy_conflict_documents',
        'legacy_conflict_legacy','legacy_conflict_dest')) {
        Remove-OwnedTree (Join-Path $root $name)
    }
    foreach ($name in @('bin\dle_user','bin\cookies_youtube.txt','bin\cookies_youtube.txt.meta',
                        'bin\cookies_twitter.txt','bin\cookies_twitter.txt.meta',
                        'bin\cookies.txt','bin\cookies.txt.meta','bin\cookies_youtube.txt.tmp',
                        'bin\cookies_twitter.txt.tmp')) {
        Remove-OwnedTree (Join-Path $root $name) $false
    }
}
foreach ($profilePath in $profiles) {
    Remove-OwnedTree (Join-Path $profilePath 'AppData\Local\Temp\fluentytdl_auth') $false
}
if ($failures.Count) {
    $failures | Write-Error -ErrorAction Continue
    exit 1
}
# Keep discovery records until runtime cleanup succeeds, so a retry still finds custom roots.
foreach ($root in $roots | Select-Object -Unique) {
    Remove-OwnedTree (Join-Path $root '.fluentytdl-data-owner.json') $false
}
if ($failures.Count) {
    $failures | Write-Error -ErrorAction Continue
    exit 1
}
foreach ($registration in $registrations) { Remove-OwnedTree $registration $false }
if ($failures.Count) {
    $failures | Write-Error -ErrorAction Continue
    exit 1
}
exit 0
