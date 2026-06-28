<#
.SYNOPSIS
  ipaddisplay M3 (capture) + M4 (encode) -- the FFmpeg SHORTCUT realization.

  GPU capture (ddagrab) -> CPU RGB->YUV color convert (libswscale) -> NV12 ->
  h264_nvenc. Produces a correctness-first low-latency Annex-B H.264 elementary
  stream for the downstream M5 NAL pump / iPad.

  This is NOT the final design. The hand-rolled C++ DXGI + zero-copy NVENC path
  is DEFERRED (no MSVC toolchain yet). The full-GPU zero-copy ddagrab->cuda->nvenc
  path is also deferred because NVENC's internal RGB->YUV matrix is uncontrolled
  (the classic 601-vs-709 bug); see docs/m3-m4-capture-encode.md.

  RUN IN AN INTERACTIVE, LOGGED-IN WINDOWS DESKTOP SESSION.
  ddagrab fails under WSL interop / SSH / service contexts.

.NOTES
  Requires ffmpeg >= 6.1 (ddagrab) with h264_nvenc. GTX 970 = H.264 8-bit only.
#>

[CmdletBinding()]
param(
    # Seconds to capture for a test dump. Set 0 for unbounded (Ctrl+C to stop).
    [int]    $Duration   = 5,
    # CBR target bitrate.
    [string] $Bitrate    = "80M",
    # Capture / encode frame rate cap.
    [int]    $Fps        = 60,
    # ddagrab output index: 0 = primary monitor. Raise for other outputs.
    [int]    $OutputIdx  = 0,
    # Bake the cursor into the frame (production ships cursor as metadata).
    [int]    $DrawMouse  = 1,
    # Output .h264 dump path.
    [string] $OutFile    = "$env:USERPROFILE\ipaddisplay-spike\capture.h264",
    # Force the slow gdigrab CPU fallback even if ddagrab is present.
    [switch] $UseGdigrab,
    # ffmpeg / ffprobe locations (override if not on PATH).
    [string] $FFmpeg     = "ffmpeg",
    [string] $FFprobe    = "ffprobe"
)

$ErrorActionPreference = "Stop"

# ---- 1. Locate ffmpeg -------------------------------------------------------
function Resolve-Tool([string]$name, [string]$fallback) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path $fallback) { return $fallback }
    throw "Could not find '$name'. Put it on PATH or pass -FFmpeg / -FFprobe. (gyan.dev or BtbN full build, >= 6.1)"
}
$FFmpeg  = Resolve-Tool $FFmpeg  "C:\ffmpeg\bin\ffmpeg.exe"
$FFprobe = Resolve-Tool $FFprobe "C:\ffmpeg\bin\ffprobe.exe"
Write-Host "ffmpeg  : $FFmpeg"
Write-Host "ffprobe : $FFprobe"

# ---- 2. Detect ddagrab ------------------------------------------------------
$hasDdagrab = $false
try {
    $filters = & $FFmpeg -hide_banner -filters 2>&1 | Out-String
    $hasDdagrab = ($filters | Select-String -Quiet "ddagrab")
} catch { }
Write-Host "ddagrab : $(if ($hasDdagrab) { 'available' } else { 'NOT FOUND -> will use gdigrab CPU fallback' })"

# ---- 3. Ensure output directory --------------------------------------------
$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

# ---- 4. Build the color + VUI filter chain ----------------------------------
# THE LOAD-BEARING PAIR (see doc):
#   scale ... out_color_matrix=bt709:out_range=full  -> the ACTUAL RGB->YUV math (libswscale)
#   setparams ...                                     -> the VUI TAG ONLY (what NVENC copies into SPS)
# They agree by construction, so the 601-vs-709 / PC-vs-TV trap cannot occur.
# NOTE: output-side -color_primaries / -color_trc are SILENTLY DROPPED by the
# validated build (come out as 2=unspecified). setparams is REQUIRED to get
# the primaries/transfer into the SPS.
# COLOR PRIMARIES = bt709 (sRGB), NOT P3: the Windows desktop framebuffer is
# sRGB-primary. We tag the stream with its TRUE source colorimetry (BT.709
# primaries + sRGB transfer) and let the iPad color-manage sRGB->Display-P3.
# Tagging P3 here would make the iPad read sRGB pixels as P3 -> oversaturation
# (the exact bug a color tool must avoid). A real sRGB->P3 gamut conversion in
# the pipeline (then tag P3) is the alternative; deferred, no accuracy benefit.
$scaleCsc  = "scale=in_range=full:out_range=full:out_color_matrix=bt709:flags=full_chroma_int+accurate_rnd,format=nv12"
$setparams = "setparams=range=pc:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709"

# ---- 5. Common encode args (CBR, low-latency, no B-frames, self-contained IDR) ----
$vbv = [string]([math]::Round([double]($Bitrate -replace 'M','') * 1000 / $Fps)) + "k"  # ~1-frame VBV
$durArgs = if ($Duration -gt 0) { @("-t",[string]$Duration) } else { @() }

# ---- 6. Run ----------------------------------------------------------------
if ($hasDdagrab -and -not $UseGdigrab) {
    # PRIMARY: GPU capture + CPU color convert + NVENC.
    $fc = "ddagrab=output_idx=${OutputIdx}:framerate=${Fps}:draw_mouse=${DrawMouse},hwdownload,format=bgra,$scaleCsc,$setparams"
    $args = @("-hide_banner","-y","-init_hw_device","d3d11va","-filter_complex",$fc,
              "-c:v","h264_nvenc","-preset","p4","-tune","ull","-profile:v","high","-level","auto","-aud","1",
              "-rc","cbr","-b:v",$Bitrate,"-maxrate",$Bitrate,"-bufsize",$vbv,
              "-bf","0","-g",[string]$Fps,"-no-scenecut","1","-forced-idr","1","-zerolatency","1","-rc-lookahead","0") + $durArgs + @("-f","h264",$OutFile)
    Write-Host "`n[PRIMARY ddagrab] $FFmpeg $($args -join ' ')`n"
    & $FFmpeg @args
}
else {
    # FALLBACK: gdigrab CPU capture (slow, high CPU, may miss 60fps at 5.6MP, skips some HW/DRM windows).
    # Same CSC + VUI chain. -i desktop grabs the whole virtual desktop; add
    #   -offset_x N -offset_y N -video_size WxH   to grab a sub-region.
    $vf = "$scaleCsc,$setparams"
    $args = @("-hide_banner","-y","-f","gdigrab","-framerate",[string]$Fps,"-i","desktop",
              "-vf",$vf,
              "-c:v","h264_nvenc","-preset","p4","-tune","ull","-profile:v","high","-level","auto","-aud","1",
              "-rc","cbr","-b:v",$Bitrate,"-maxrate",$Bitrate,"-bufsize",$vbv,
              "-bf","0","-g",[string]$Fps,"-no-scenecut","1","-forced-idr","1","-zerolatency","1","-rc-lookahead","0") + $durArgs + @("-f","h264",$OutFile)
    Write-Host "`n[FALLBACK gdigrab] $FFmpeg $($args -join ' ')`n"
    & $FFmpeg @args
}

if ($LASTEXITCODE -ne 0) { throw "ffmpeg exited with code $LASTEXITCODE" }
Write-Host "`nWrote: $OutFile`n"

# ---- 7. Echo the verify commands -------------------------------------------
Write-Host "==== VERIFY (paste these) ===================================================="
Write-Host @"
# Stream summary (expect profile=High width=2732 pix_fmt=yuv420p (or yuvj420p)
#   color_range=pc color_space=bt709 color_primaries=bt709 color_transfer=iec61966-2-1;
#   level is whatever NVENC's max supported on GM204, likely 51):
& '$FFprobe' -v error -select_streams v:0 -show_entries stream=profile,level,width,height,coded_width,coded_height,pix_fmt,color_range,color_space,color_primaries,color_transfer,has_b_frames -of default=noprint_wrappers=1 '$OutFile'

# AUTHORITATIVE SPS / VUI / crop check (expect pic_width_in_mbs_minus1=170 => 2736 coded,
#   frame_crop_right_offset=2 => 4px crop => 2732, video_full_range_flag=1,
#   colour_primaries=1 transfer_characteristics=13 matrix_coefficients=1).
#   Use default verbosity -- do NOT add -v trace (floods ~10M lines):
& '$FFmpeg' -hide_banner -i '$OutFile' -c copy -bsf:v trace_headers -f null - 2>&1 | Select-String -Pattern 'profile_idc|level_idc|chroma_format_idc|pic_width_in_mbs|pic_height_in_map|frame_mbs_only|frame_cropping_flag|frame_crop_|video_full_range_flag|colour_primaries|transfer_characteristics|matrix_coefficients' | Select-Object -First 17

# Decode the whole dump (must exit 0, no errors):
& '$FFmpeg' -hide_banner -v error -xerror -i '$OutFile' -f null -

# Frame types (expect one I then all P, ZERO B):
& '$FFprobe' -v error -select_streams v:0 -show_entries frame=pict_type -of csv=p=0 -read_intervals '%+#120' '$OutFile'
"@
