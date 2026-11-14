<#
  SELENO: bring the assemblies, print files and review copies up to date after a part is edited in Inventor.

  Usage (from any PowerShell window):
    powershell -ExecutionPolicy Bypass -File Z:\ASTRA\inventor\tools\update_print_files.ps1          # only what is out of date
    ... update_print_files.ps1 -All        # re-export every printed part
    ... update_print_files.ps1 -Check      # also run the clash check on the full assembly
    ... update_print_files.ps1 -NoStep     # skip the (slow, ~1 min) assembly STEP

  What it does:
    1. Any printed part (.ipt in mount\drive) newer than its STL in export\stl is re-exported, and copied into final\.
    2. SelenoCompact_RAmotor.iam and CG5_SelenoCompact_Motors.iam are updated + saved when a part is newer than them
       (not saved if you have them open in Inventor; they update themselves there anyway).
    3. The assembly STEP is re-exported if any part or assembly under mount\ is newer than it (this also updates the C8N top).
    4. SelenoParts.zip gets any STL / STEP that differs from its copy in the zip.
  Save your changes in Inventor first. The script reads the saved files and stops if one of them has unsaved changes.
  Each Inventor step runs in its own hidden Inventor. If it hasn't got past start-up in 35 s it's killed and retried once,
  and every step has a hard cap (about 1-2 min). A typical run after a frame edit takes about 2 minutes.
#>
param([switch]$All, [switch]$Check, [switch]$NoStep, [string]$Stage = '', [string]$Parts = '', [string]$Log = '')
$ErrorActionPreference = 'Stop'

$ROOT = 'Z:\ASTRA\inventor'
$DRV = "$ROOT\mount\drive"
$STL = "$ROOT\export\stl"
$FINAL = "$ROOT\final"
$ZIP = "$ROOT\export\SelenoParts.zip"
$STEPF = "$ROOT\export\SELENO_CG5_C8N_assembly.step"
$ASM_SEL = "$DRV\SelenoCompact_RAmotor.iam"
$ASM_TOP = "$ROOT\mount\CG5_SelenoCompact_Motors.iam"
$ASM_C8N = "$ROOT\mount\CG5_SelenoCompact_Motors_C8N.iam"
# printed part -> STL name. Manual = the STL can't be made from the part alone (it holds 2 copies), so it is only flagged.
$PRINTED = @(
  @{ ipt = 'frame_compact_connectors_RAmotor'; stl = 'Frame' },
  @{ ipt = 'base_plate_compact_orange'; stl = 'Base_plate' },
  @{ ipt = 'ra_motor_bracket'; stl = 'RA_bracket' },
  @{ ipt = 'ra_worm_pulley_40T_printed'; stl = 'RA_worm_pulley_40T' },
  @{ ipt = 'dec_motor_bracket'; stl = 'Dec_bracket' },
  @{ ipt = 'dec_clamp_cap'; stl = 'Dec_clamp_cap' },
  @{ ipt = 'dec_band2_cap'; stl = 'Dec_band_cap_x2'; manual = $true }
)
$WORKDIR = Join-Path $env:TEMP 'seleno_update'

# ======================================================================= worker (one stage, inside a hidden Inventor)
if ($Stage) {
  function Say($s) { Write-Output $s }
  $inv = New-Object -ComObject Inventor.Application; $inv.SilentOperation = $true; Say 'inventor up'
  try {
    switch ($Stage) {
      'parts' {
        $tr = $inv.ApplicationAddIns.ItemById('{533E9A98-FC3B-11D4-8E7E-0010B541CD80}')
        if (-not $tr.Activated) {  # $tr.Activate() hangs in a hidden Inventor on some days; a plain SaveAs .stl loads the translator reliably
          $n0 = (($Parts -split ',')[0] -split '=')[0]; $w = $inv.Documents.Open("$DRV\$n0.ipt", $false); $w.SaveAs("$WORKDIR\warm.stl", $true); $w.Close($true) }
        Say 'stl translator ready'
        foreach ($p in ($Parts -split ',')) {
          $n, $s = $p -split '='
          $d = $inv.Documents.Open("$DRV\$n.ipt", $false); $d.Update(); if ($d.Dirty) { $d.Save() }
          $ctx = $inv.TransientObjects.CreateTranslationContext(); $ctx.Type = 13059; $op = $inv.TransientObjects.CreateNameValueMap()
          [void]$tr.HasSaveCopyAsOptions($d, $ctx, $op); $op.Value('ExportUnits') = 5; $op.Value('Resolution') = 0; $op.Value('OutputFileType') = 0
          $dm = $inv.TransientObjects.CreateDataMedium(); $dm.FileName = "$STL\$s.stl"; $tr.SaveCopyAs($d, $ctx, $op, $dm)
          Say ("  {0}.stl  ({1:N0} bytes, {2:N2} cm3)" -f $s, (Get-Item "$STL\$s.stl").Length, $d.ComponentDefinition.MassProperties.Volume)
          $d.Close($true)
        }
      }
      'asm' {
        foreach ($g in ($Parts -split '\|')) {
          $noSave = $g.EndsWith('*'); $f = $g.TrimEnd('*')
          $a = $inv.Documents.Open($f, $false); $a.Update()
          if ($noSave) { Say "  checked $(Split-Path $f -Leaf) (open in your Inventor, not saved)" } else { $a.Save(); Say "  updated + saved $(Split-Path $f -Leaf)" }
          if ($Check -and $f -eq $ASM_TOP) {
            $acd = $a.ComponentDefinition; $sel = $null; foreach ($o in $acd.Occurrences) { if ($o.Definition.Document.FullFileName -eq $ASM_SEL) { $sel = $o } }
            $rest = $inv.TransientObjects.CreateObjectCollection(); foreach ($o in $acd.Occurrences) { if ($o.Name -ne $sel.Name -and $o.Visible) { [void]$rest.Add($o) } }
            $one = $inv.TransientObjects.CreateObjectCollection(); [void]$one.Add($sel)
            $known = 'Flat bit bottom-lip screw M4|RA frame nut M3|Screen screw M2x5'
            $res = $acd.AnalyzeInterference($one, $rest); $new = 0
            foreach ($r in $res) { $v = $r.Volume * 1000; if ($v -lt 0.01) { continue }
              $k = ($r.OccurrenceOne.Name + ' ' + $r.OccurrenceTwo.Name) -match $known; if (-not $k) { $new++ }
              Say ("  clash {0} <-> {1}  {2:N2} mm3{3}" -f $r.OccurrenceOne.Name, $r.OccurrenceTwo.Name, $v, $(if ($k) { '  (known, by design)' } else { '  <-- NEW' })) }
            Say "  clash check: $new new"
          }
          $a.Close($true)
        }
      }
      'step' {
        $ad = $inv.ApplicationAddIns.ItemById('{90AF7F40-0C01-11D5-8E83-0010B541CD80}')
        if (-not $ad.Activated) {  # Activate() can hang in a hidden Inventor; a small part's SaveAs .stp loads the translator reliably
          $w = $inv.Documents.Open("$DRV\base_plate_compact_orange.ipt", $false); $w.SaveAs("$WORKDIR\warm.stp", $true); $w.Close($true) }
        Say 'step translator ready'
        $doc = $inv.Documents.Open($ASM_C8N, $false); Say '  opened top assembly'
        $ctx = $inv.TransientObjects.CreateTranslationContext(); $ctx.Type = 13059; $op = $inv.TransientObjects.CreateNameValueMap()
        if ($ad.HasSaveCopyAsOptions($doc, $ctx, $op)) { $op.Value('ApplicationProtocolType') = 3 }
        $tmp = "$WORKDIR\assembly.step"; if (Test-Path $tmp) { Remove-Item $tmp }
        $dm = $inv.TransientObjects.CreateDataMedium(); $dm.FileName = $tmp; $ad.SaveCopyAs($doc, $ctx, $op, $dm)
        Copy-Item $tmp $STEPF -Force; Say ("  STEP {0:N0} bytes" -f (Get-Item $STEPF).Length); $doc.Close($true)
      }
    }
    Say 'STAGE OK'
  } finally { $inv.Quit() }
  return
}

# ======================================================================= driver
New-Item -ItemType Directory -Force $WORKDIR | Out-Null
# Timeouts (s): $start = Inventor must start AND get through its first step (translator / first open) in this time
# (every hang seen so far was there); $max = hard cap for the whole stage. A hang is killed at once and retried once.
function Run-Stage($name, $extra, [int]$max = 90, [int]$start = 35) {
  for ($try = 1; $try -le 2; $try++) {
    $log = "$WORKDIR\$name.log"; Remove-Item $log, "$log.err" -ErrorAction SilentlyContinue
    $args2 = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Stage', $name) + $extra; if ($Check) { $args2 += '-Check' }
    $t0 = Get-Date
    $p = Start-Process powershell -ArgumentList $args2 -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru -WindowStyle Hidden
    $stalled = $false
    while (-not $p.HasExited) {
      Start-Sleep -Seconds 1; $el = ((Get-Date) - $t0).TotalSeconds
      $lines = @(Get-Content $log -ErrorAction SilentlyContinue).Count
      if (($lines -lt 2 -and $el -gt $start) -or $el -gt $max) { $stalled = $true; break }
    }
    function Mine { @(Get-Process Inventor -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -gt $t0 -and -not $_.MainWindowTitle }) }
    if ($stalled) { Stop-Process -Id $p.Id -Force -Confirm:$false -ErrorAction SilentlyContinue; Mine | Stop-Process -Force -Confirm:$false }
    else {  # finished normally: give Inventor's own Quit() a moment (a hard kill mid-shutdown pops the crash reporter)
      $w = 0; while ((Mine).Count -gt 0 -and $w -lt 15) { Start-Sleep 1; $w++ }; Mine | Stop-Process -Force -Confirm:$false }
    $out = @(Get-Content $log -ErrorAction SilentlyContinue)
    if ($out -contains 'STAGE OK') { $out | Where-Object { $_ -like '  *' } | ForEach-Object { Write-Host $_ }; return $true }
    Write-Host ("  [$name] attempt $try " + $(if ($stalled) { "hung, killed after {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds } else { 'failed' }) + $(if ($try -eq 1) { ', retrying' } else { '' }))
    Get-Content "$log.err" -ErrorAction SilentlyContinue | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" }
  }
  return $false
}

# --- refuse to export over unsaved edits in the user's own Inventor
$openInUser = @{}
try {
  $u = [Runtime.InteropServices.Marshal]::GetActiveObject('Inventor.Application')
  foreach ($d in $u.Documents) {
    $fn = $d.FullFileName; if (-not $fn.StartsWith("$ROOT\mount", 'OrdinalIgnoreCase')) { continue }
    $openInUser[$fn.ToLower()] = $true
    if ($d.Dirty -and $d.DocumentType -eq 12290) { throw "Unsaved changes in $(Split-Path $fn -Leaf) - save it in Inventor, then run this again." }
  }
} catch [System.Runtime.InteropServices.COMException] { } # no Inventor running
"SELENO print-file update  " + (Get-Date -Format 'yyyy-MM-dd HH:mm')

# --- 1. printed parts
$todo = @(); $flag = @()
foreach ($p in $PRINTED) {
  $src = Get-Item "$DRV\$($p.ipt).ipt"; $dst = "$STL\$($p.stl).stl"
  $stale = $All -or -not (Test-Path $dst) -or $src.LastWriteTime -gt (Get-Item $dst).LastWriteTime
  if ($stale -and $p.manual) { $flag += "$($p.stl).stl is older than $($p.ipt).ipt (holds 2 copies, re-export by hand)" }
  elseif ($stale) { $todo += $p }
  foreach ($m in @(Get-ChildItem "$ROOT\export" -Recurse -Filter "$($p.stl).3mf" -ErrorAction SilentlyContinue)) {
    if ($src.LastWriteTime -gt $m.LastWriteTime) { $flag += "$($m.FullName.Substring($ROOT.Length + 1)) (your slicer project) is older than the part - re-slice it" } }
}
$zipAdd = @()
if ($todo.Count) {
  "STL export: " + (($todo | ForEach-Object { $_.stl }) -join ', ')
  $ok = Run-Stage 'parts' @('-Parts', (($todo | ForEach-Object { "$($_.ipt)=$($_.stl)" }) -join ',')) (30 + 10 * $todo.Count)
  if (-not $ok) { throw 'STL export failed twice, see ' + $WORKDIR }
  foreach ($p in $todo) { $zipAdd += , @("stl\$($p.stl).stl", "$STL\$($p.stl).stl") }
} else { 'STLs: all up to date' }

# --- 2. final\ review copies (every printed part whose file differs)
foreach ($p in $PRINTED) {
  $src = "$DRV\$($p.ipt).ipt"; $dst = "$FINAL\$($p.ipt).ipt"
  if (-not (Test-Path $dst) -or (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash) { Copy-Item $src $dst -Force; "final\: copied $($p.ipt).ipt" }
}

# --- 3. assemblies (C8N in its own Inventor: opening it after the motors assembly in one session throws)
function Tag($f) { if ($openInUser.ContainsKey($f.ToLower())) { "$f*" } else { $f } }   # * = open in the user's Inventor: update/check only, don't save
foreach ($f in @($ASM_SEL, $ASM_TOP, $ASM_C8N)) { if ($openInUser.ContainsKey($f.ToLower())) { "assembly open in your Inventor, not saved from here: $(Split-Path $f -Leaf)" } }
# skipped when no part/sub-assembly under mount\ is newer than the saved assemblies (and no -Check)
$asmT = [datetime]::MaxValue; foreach ($f in @($ASM_SEL, $ASM_TOP)) { $t = (Get-Item $f).LastWriteTime; if ($t -lt $asmT) { $asmT = $t } }
$asmStale = @(Get-ChildItem "$ROOT\mount" -Recurse -Include *.ipt, *.iam | Where-Object { $_.FullName -notmatch '\\OldVersions' -and $_.FullName -notin @($ASM_SEL, $ASM_TOP, $ASM_C8N) -and $_.LastWriteTime -gt $asmT }).Count -gt 0
$asmTodo = @((Tag $ASM_SEL)); if ($Check -or -not $openInUser.ContainsKey($ASM_TOP.ToLower())) { $asmTodo += (Tag $ASM_TOP) }
$asmTodo = @($asmTodo | Where-Object { -not $_.EndsWith('*') -or ($Check -and $_ -eq "$ASM_TOP*") })
if (-not ($asmStale -or $All -or $Check)) { 'assemblies: up to date' }
elseif ($asmTodo.Count) { 'assemblies: updating' + $(if ($Check) { ' + clash check' } else { '' }); if (-not (Run-Stage 'asm' @('-Parts', ($asmTodo -join '|')) 100)) { 'WARNING: assembly update failed' } }
# the C8N top assembly isn't saved here: the STEP export below opens and updates it. Just keep final\ in step.
if ((Get-FileHash $ASM_C8N).Hash -ne (Get-FileHash "$FINAL\CG5_SelenoCompact_Motors_C8N.iam").Hash) { Copy-Item $ASM_C8N "$FINAL\" -Force; 'final\: copied CG5_SelenoCompact_Motors_C8N.iam' }

# --- 4. STEP
if (-not $NoStep) {
  $stepT = if (Test-Path $STEPF) { (Get-Item $STEPF).LastWriteTime } else { [datetime]::MinValue }
  $newer = @(Get-ChildItem "$ROOT\mount" -Recurse -Include *.ipt, *.iam | Where-Object { $_.FullName -notmatch '\\OldVersions' -and $_.LastWriteTime -gt $stepT.AddSeconds(-1) -and $_.FullName -notin @($ASM_SEL, $ASM_TOP, $ASM_C8N) })
  if ($All -or $todo.Count -or $newer.Count) {
    'STEP: exporting'; if (Run-Stage 'step' @() 120) { $zipAdd += , @('SELENO_CG5_C8N_assembly.step', $STEPF) } else { 'WARNING: STEP export failed' }
  } else { 'STEP: up to date' }
}

# --- 5. zip: any STL / STEP whose copy in the zip differs in size or is older (also catches an earlier interrupted run)
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$want = @(); foreach ($p in $PRINTED) { $want += , @("stl\$($p.stl).stl", "$STL\$($p.stl).stl") }; $want += , @('SELENO_CG5_C8N_assembly.step', $STEPF)
$z = [System.IO.Compression.ZipFile]::Open($ZIP, 'Update'); $upd = @()
try {
  foreach ($e in $want) {
    if (-not (Test-Path $e[1])) { continue }; $fi = Get-Item $e[1]; $old = $z.GetEntry($e[0])
    if ($old -and $old.Length -eq $fi.Length -and $old.LastWriteTime.DateTime -ge $fi.LastWriteTime.AddSeconds(-2)) { continue }
    if ($old) { $old.Delete() }; [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($z, $e[1], $e[0]); $upd += $e[0]
  }
} finally { $z.Dispose() }
if ($upd.Count) { "SelenoParts.zip: updated " + ($upd -join ', ') } else { 'SelenoParts.zip: up to date' }
foreach ($f in $flag) { "NOTE: $f" }
'done'
