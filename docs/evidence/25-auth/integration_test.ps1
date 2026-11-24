$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8002/api/v1"
$script:results = @()

function Check($name, $actual, $expected) {
    $pass = if ($actual -eq $expected) { "PASS" } else { "FAIL" }
    $script:results += [pscustomobject]@{ Step=$name; Expected=$expected; Actual=$actual; Result=$pass }
    Write-Host "[$pass] $name -> expected $expected, got $actual"
}

function Send($method, $path, $headers, $body) {
    try {
        $params = @{ Method = $method; Uri = "$base$path"; Headers = $headers; UseBasicParsing = $true }
        if ($body) { $params.Body = ($body | ConvertTo-Json -Compress); $params.ContentType = "application/json" }
        $r = Invoke-WebRequest @params
        return [pscustomobject]@{ Status = [int]$r.StatusCode; Body = $r.Content }
    } catch {
        $resp = $_.Exception.Response
        $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
        return [pscustomobject]@{ Status = $code; Body = "" }
    }
}

# 1. unauthenticated request
Check "no-token business request" (Send "POST" "/chat" @{} @{ message = "hi" }).Status 401

# 2. wrong password
Check "wrong password login" (Send "POST" "/auth/login" @{} @{ user_id = "u_demo"; password = "wrong" }).Status 401

# 3. correct login
$login = Send "POST" "/auth/login" @{} @{ user_id = "u_demo"; password = "Buyer-Demo-2026" }
Check "correct login" $login.Status 200
$token = ($login.Body | ConvertFrom-Json).access_token
$auth = @{ Authorization = "Bearer $token" }

# 4. current identity
$me = Send "GET" "/auth/me" $auth
Check "query current identity" $me.Status 200
Write-Host "       identity -> $($me.Body)"

# 5. forged user_id in body is ignored, session is created under token identity
$chat = Send "POST" "/chat" $auth @{ message = "你好"; user_id = "forged_other" }
Check "forged body user_id ignored, chat works" $chat.Status 200
$sessionId = ($chat.Body | ConvertFrom-Json).session_id

# 6. buyer hits admin endpoint
Check "buyer accesses admin endpoint" (Send "GET" "/admin/tools" $auth $null).Status 403

# 7. forged token
Check "forged token rejected" (Send "GET" "/auth/me" @{ Authorization = "Bearer not-a-real-token" } $null).Status 401

# 8. logout then replay
Check "logout" (Send "POST" "/auth/logout" $auth $null).Status 204
Check "old token after logout" (Send "GET" "/auth/me" $auth $null).Status 401

# 9. verify ownership at database level
$py = "D:\code\CaseDesk\.venv\Scripts\python.exe"
$verify = & $py "C:\Users\7000\AppData\Local\Temp\verify_session.py" $sessionId
Write-Host "       DB ownership -> $verify"
if ($verify -eq "u_demo") {
    Write-Host "[PASS] session ownership in DB is u_demo"
} else {
    Write-Host "[FAIL] session ownership is $verify"
}

Write-Host "`n==== summary ===="
$script:results | Format-Table -AutoSize
if ($script:results.Result -contains "FAIL") { exit 1 } else { Write-Host "ALL CHECKS PASSED" }
