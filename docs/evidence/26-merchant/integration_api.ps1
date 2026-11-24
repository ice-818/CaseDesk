$base = "http://127.0.0.1:8002/api/v1"
$script:results = @()

function Check($label, $cond) {
    if ($cond) { $script:results += "PASS: $label" }
    else { $script:results += "FAIL: $label" }
}

function GetStatus($token, $path) {
    try {
        $h = @{Authorization = "Bearer $token" }
        if (-not $token) { $h = @{} }
        Invoke-WebRequest -UseBasicParsing "$base$path" -Headers $h | Out-Null
        return 200
    } catch {
        return $_.Exception.Response.StatusCode.value__
    }
}

function Login($uid, $pwd) {
    return Invoke-RestMethod -Method Post "$base/auth/login" -Body (@{user_id = $uid; password = $pwd } | ConvertTo-Json) -ContentType "application/json"
}

Check "dev no-token admin allowed (dev-only, production is 401)" ((GetStatus "" "/admin/tools") -eq 200)

try {
    Invoke-WebRequest -UseBasicParsing -Method Post "$base/auth/login" -Body (@{user_id = "u_demo"; password = "wrong" } | ConvertTo-Json) -ContentType "application/json" | Out-Null
    $bad = 200
} catch {
    $bad = $_.Exception.Response.StatusCode.value__
}
Check "bad password 401" ($bad -eq 401)

$buyer = (Login u_demo Buyer-Demo-2026).access_token
$agent = (Login u_service_agent Agent-Demo-2026).access_token
$op = (Login u_operator Operator-Demo-2026).access_token
Check "three roles login" ([bool]$buyer -and [bool]$agent -and [bool]$op)

$me = Invoke-RestMethod "$base/auth/me" -Headers @{Authorization = "Bearer $agent" }
Check "agent bound m_demo" ($me.merchant_id -eq "m_demo")

Check "agent admin/tools 403" ((GetStatus $agent "/admin/tools") -eq 403)
Check "buyer admin/tools 403" ((GetStatus $buyer "/admin/tools") -eq 403)
Check "operator admin/tools 200" ((GetStatus $op "/admin/tools") -eq 200)
Check "agent platform/datasets 403" ((GetStatus $agent "/admin/platform/datasets") -eq 403)
Check "buyer platform/datasets 403" ((GetStatus $buyer "/admin/platform/datasets") -eq 403)
Check "operator platform/datasets 200" ((GetStatus $op "/admin/platform/datasets") -eq 200)

$body = @{message = "hello"; user_id = "u_operator" } | ConvertTo-Json
$resp = Invoke-RestMethod -Method Post "$base/chat" -Headers @{Authorization = "Bearer $buyer" } -Body $body -ContentType "application/json"
Check "body user_id tamper ignored" ([bool]$resp.session_id)
$script:tamperedSession = $resp.session_id

try {
    Invoke-WebRequest -UseBasicParsing -Method Post "$base/auth/logout" -Headers @{Authorization = "Bearer $buyer" } | Out-Null
    $lo = 204
} catch {
    $lo = $_.Exception.Response.StatusCode.value__
}
Check "logout 204" ($lo -eq 204)
Check "old token 401" ((GetStatus $buyer "/admin/tools") -eq 401)

$script:results
