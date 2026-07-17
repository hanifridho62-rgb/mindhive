# user_prompt_submit — the prompt-quality gate. Reads the UserPromptSubmit
# payload on stdin and, when the typed prompt is underspecified AND trained
# trigger rules exist (rules/triggers.json), emits additionalContext JSON on
# stdout so the agent front-loads the proven skill (plan/tests/review) instead
# of improvising on a vague instruction. Every other case prints nothing and
# exits 0. Additive-only (never blocks a prompt), fail-open + hang-proof: any
# error means no injection. stdout IS the hook's context channel, so this
# script must never write anything to it except the Python context JSON.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: never self-trigger
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> silent
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $out = ($raw | & $py -m mindhive hook user_prompt_submit)
    if ($out) { Write-Output $out }                     # context -> Claude; empty -> silent
} catch {}
exit 0
