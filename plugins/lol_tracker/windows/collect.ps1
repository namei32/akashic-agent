param([Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$started = [DateTimeOffset]::UtcNow
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$cacheRoot = Join-Path $Root 'match-cache'
New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null

function Write-AtomicJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 30 -Compress), [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
}

function Cache-Key([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Read-League([string]$Path) {
    if (([DateTimeOffset]::UtcNow - $started).TotalSeconds -gt 210) { throw 'collector_budget_exceeded' }
    # Credentials remain in Windows memory and curl stdin; never argv, files, or logs.
    $inputText = 'url = "https://127.0.0.1:' + $script:lcuPort + $Path + '"' + "`n" +
        'user = "riot:' + $script:lcuToken + '"' + "`n" +
        'insecure' + "`n" + 'silent' + "`n" + 'show-error' + "`n" +
        'connect-timeout = 3' + "`n" + 'max-time = 12' + "`n" + 'noproxy = "*"'
    $raw = $inputText | & curl.exe --config - 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'client_transport_failed' }
    $value = $raw | ConvertFrom-Json
    if ($value -isnot [array] -and ($value.httpStatus -ge 400 -or $value.errorCode)) { throw ('client_http_' + $value.httpStatus) }
    return $value
}

function Normalize-Match($Game, $Player, $Heroes, $QueueNames) {
    $identity = @($Game.participantIdentities | Where-Object { $_.player.puuid -eq $Player.puuid })
    if ($identity.Count -ne 1) { throw 'participant_identity_mismatch' }
    $participant = @($Game.participants | Where-Object { $_.participantId -eq $identity[0].participantId })[0]
    if (-not $participant -or $Game.platformId -ne $Player.platform) { throw 'match_platform_mismatch' }
    $stats = $participant.stats
    $duration = [double]$Game.gameDuration
    if ($duration -le 0 -or -not $Game.gameCreationDate) { throw 'invalid_match_duration' }
    $begin = [DateTimeOffset]::Parse($Game.gameCreationDate).ToUniversalTime()
    $team = @($Game.participants | Where-Object { $_.teamId -eq $participant.teamId })
    $teamKills = ($team | ForEach-Object { $_.stats.kills } | Measure-Object -Sum).Sum
    $teamDamage = ($team | ForEach-Object { $_.stats.totalDamageDealtToChampions } | Measure-Object -Sum).Sum
    $completeTeam = ($Game.participants.Count -ge 10 -and $team.Count -ge 5)
    $queueId = [string]$Game.queueId
    if (-not $QueueNames.ContainsKey($queueId)) {
        try { $QueueNames[$queueId] = (Read-League ('/lol-game-queues/v1/queues/' + $queueId)).name }
        catch { $QueueNames[$queueId] = [string]$Game.gameMode + ' (' + $queueId + ')' }
    }
    $champion = $Heroes[[string]$participant.championId]
    if (-not $champion) { $champion = 'Champion ' + $participant.championId }
    $cs = [int]$stats.totalMinionsKilled + [int]$stats.neutralMinionsKilled
    return [ordered]@{
        match_id=[string]$Game.gameId; platform=[string]$Game.platformId; puuid=[string]$Player.puuid
        started_at=$begin.ToString('o'); ended_at=$begin.AddSeconds($duration).ToString('o')
        duration_seconds=$duration; queue_id=[int]$Game.queueId; queue_name=$QueueNames[$queueId]
        game_mode=[string]$Game.gameMode; champion_id=[int]$participant.championId; champion=$champion
        position=[string]$participant.timeline.lane; win=[bool]$stats.win
        kills=[int]$stats.kills; deaths=[int]$stats.deaths; assists=[int]$stats.assists
        damage=[int64]$stats.totalDamageDealtToChampions; gold=[int]$stats.goldEarned
        cs=$cs; cs_per_minute=[Math]::Round($cs * 60 / $duration, 2); vision_score=[int]$stats.visionScore
        kill_participation=$(if ($completeTeam -and $teamKills -gt 0) { ([double]$stats.kills + $stats.assists)/$teamKills } else { $null })
        damage_share=$(if ($completeTeam -and $teamDamage -gt 0) { [double]$stats.totalDamageDealtToChampions/$teamDamage } else { $null })
        remake=[bool]$stats.gameEndedInEarlySurrender; full_details=[bool]$completeTeam
    }
}

$snapshot = [ordered]@{schema_version=1; observed_at=$started.ToString('o'); status='offline'; error=$null; players=@()}
try {
    $config = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'watchlist.json') | ConvertFrom-Json
    if (-not $config.enabled) { $snapshot.status='disabled' }
    else {
        $client = Get-CimInstance Win32_Process -Filter "Name='LeagueClientUx.exe'" | Select-Object -First 1
        if (-not $client) { throw 'client_offline' }
        $script:lcuPort = [regex]::Match($client.CommandLine, '--app-port=(\d+)').Groups[1].Value
        $script:lcuToken = [regex]::Match($client.CommandLine, '--remoting-auth-token=([^"\s]+)').Groups[1].Value
        if (-not $script:lcuPort -or -not $script:lcuToken) { throw 'client_credentials_unavailable' }
        $heroes = @{}
        foreach ($hero in @(Read-League '/lol-game-data/assets/v1/champion-summary.json')) {
            $heroes[[string]$hero.id] = $(if ($hero.description) { $hero.description } else { $hero.name })
        }
        $queueNames = @{}
        $snapshot.status='ok'
        foreach ($watch in @($config.players)) {
            $player = [ordered]@{riot_id=$watch.riot_id; platform=$watch.platform; status='error'; error=$null; matches=@()}
            try {
                $summoner = Read-League ('/lol-summoner/v1/summoners?name=' + [Uri]::EscapeDataString($watch.riot_id))
                if (($summoner.gameName + '#' + $summoner.tagLine) -ine $watch.riot_id -or -not $summoner.puuid) {
                    throw 'player_not_found_in_current_server'
                }
                $player.puuid=[string]$summoner.puuid
                $historyGames = @()
                $historyComplete = $false
                for ($page=0; $page -lt 5; $page++) {
                    $beginIndex = $page * 20
                    $history = Read-League ('/lol-match-history/v1/products/lol/' + $summoner.puuid + '/matches?begIndex=' + $beginIndex + '&endIndex=' + ($beginIndex+19))
                    if ($history.platformId -ne $watch.platform) { throw 'client_server_mismatch' }
                    if ($null -eq $history.games -or $null -eq $history.games.games) { throw 'invalid_match_history' }
                    $batch = @($history.games.games)
                    $historyGames += $batch
                    if ($batch.Count -lt 20) { $historyComplete=$true; break }
                    if ([DateTimeOffset]::Parse($batch[-1].gameCreationDate) -lt $started.AddDays(-8)) { break }
                }
                $player.history_exhausted=$historyComplete
                $player.oldest_started_at=$(if ($historyGames.Count) { ([DateTimeOffset]::Parse($historyGames[-1].gameCreationDate)).ToUniversalTime().ToString('o') } else { $null })
                foreach ($game in $historyGames) {
                    $cachePath = Join-Path $cacheRoot ((Cache-Key ($watch.platform + ':' + $summoner.puuid + ':' + $game.gameId)) + '.json')
                    if (Test-Path -LiteralPath $cachePath) {
                        $normalized = Get-Content -Raw -Encoding UTF8 -LiteralPath $cachePath | ConvertFrom-Json
                    } else {
                        $detail = $game
                        try { $detail = Read-League ('/lol-match-history/v1/games/' + $game.gameId) } catch { }
                        $normalized = Normalize-Match $detail $player $heroes $queueNames
                        Write-AtomicJson $cachePath $normalized
                    }
                    $player.matches += $normalized
                }
                $player.status='ok'
            } catch {
                $player.error = [string]$_.Exception.Message
                $snapshot.status='partial'
            }
            $snapshot.players += $player
        }
    }
} catch {
    $snapshot.error=[string]$_.Exception.Message
}
# No local auth token or process command line is part of this public observation.
Write-AtomicJson (Join-Path $Root 'snapshot.json') $snapshot
@{status=$snapshot.status; observed_at=$snapshot.observed_at; players=$snapshot.players.Count} | ConvertTo-Json -Compress
