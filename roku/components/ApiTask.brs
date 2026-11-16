sub init()
    m.top.functionName = "execute"
end sub

sub execute()
    act = m.top.action
    if act = "login"
        m.top.result = doLogin()
    else if act = "channels"
        m.top.result = doChannels()
    else if act = "tune"
        m.top.result = doTune()
    else if act = "qc_init"
        m.top.result = doQcInit()
    else if act = "qc_poll"
        m.top.result = doQcPoll()
    else
        m.top.result = { ok: false, error: "Unknown action: " + act }
    end if
end sub

function newTransfer(url as string) as object
    xfer = CreateObject("roUrlTransfer")
    xfer.SetUrl(url)
    xfer.SetCertificatesFile("common:/certs/ca-bundle.crt")
    xfer.InitClientCertificates()
    xfer.RetainBodyOnError(true)
    return xfer
end function

function authHeader(token as string) as string
    devId = m.top.deviceId
    if devId = "" then devId = "ctv-roku"
    h = "MediaBrowser Client=" + Chr(34) + "ClassicTV Roku" + Chr(34)
    h = h + ", Device=" + Chr(34) + "Roku" + Chr(34)
    h = h + ", DeviceId=" + Chr(34) + devId + Chr(34)
    h = h + ", Version=" + Chr(34) + "1.0.0" + Chr(34)
    if token <> ""
        h = h + ", Token=" + Chr(34) + token + Chr(34)
    end if
    return h
end function

function httpGetJson(url as string, token as string) as object
    xfer = newTransfer(url)
    if token <> ""
        xfer.AddHeader("Authorization", authHeader(token))
    end if
    port = CreateObject("roMessagePort")
    xfer.SetMessagePort(port)
    if not xfer.AsyncGetToString()
        return { ok: false, status: 0, error: "Request failed to start" }
    end if
    msg = wait(10000, port)
    if type(msg) <> "roUrlEvent"
        return { ok: false, status: 0, error: "Request timed out" }
    end if
    code = msg.GetResponseCode()
    body = msg.GetString()
    if code < 200 or code >= 300
        return { ok: false, status: code, error: "HTTP " + code.ToStr() }
    end if
    parsed = ParseJson(body)
    if parsed = invalid
        return { ok: false, status: code, error: "Invalid JSON" }
    end if
    return { ok: true, status: code, data: parsed }
end function

function httpPostJson(url as string, token as string, payload as object) as object
    xfer = newTransfer(url)
    xfer.AddHeader("Content-Type", "application/json")
    xfer.AddHeader("Authorization", authHeader(token))
    port = CreateObject("roMessagePort")
    xfer.SetMessagePort(port)
    if not xfer.AsyncPostFromString(FormatJson(payload))
        return { ok: false, status: 0, error: "POST failed to start" }
    end if
    msg = wait(10000, port)
    if type(msg) <> "roUrlEvent"
        return { ok: false, status: 0, error: "Request timed out" }
    end if
    code = msg.GetResponseCode()
    body = msg.GetString()
    if code < 200 or code >= 300
        return { ok: false, status: code, error: "HTTP " + code.ToStr() }
    end if
    parsed = ParseJson(body)
    if parsed = invalid
        return { ok: false, status: code, error: "Invalid JSON" }
    end if
    return { ok: true, status: code, data: parsed }
end function

function doLogin() as object
    base = m.top.serverUrl
    url = base + "/Users/AuthenticateByName"
    resp = httpPostJson(url, "", { Username: m.top.username, Pw: m.top.password })
    if not resp.ok
        return { ok: false, status: resp.status, error: resp.error }
    end if
    d = resp.data
    user = d.User
    if d.AccessToken = invalid or user = invalid
        return { ok: false, error: "Unexpected login response" }
    end if
    return {
        ok: true,
        accessToken: d.AccessToken,
        userId: user.Id,
        username: user.Name
    }
end function

function doQcInit() as object
    base = m.top.serverUrl
    resp = httpPostJson(base + "/QuickConnect/Initiate", "", {})
    if not resp.ok
        return { ok: false, status: resp.status, error: resp.error }
    end if
    d = resp.data
    if d.Secret = invalid or d.Code = invalid
        return { ok: false, error: "Unexpected Quick Connect response" }
    end if
    return { ok: true, secret: d.Secret, code: d.Code }
end function

function doQcPoll() as object
    base = m.top.serverUrl
    sec = CreateObject("roUrlTransfer").Escape(m.top.qcSecret)
    check = httpGetJson(base + "/QuickConnect/Connect?secret=" + sec, "")
    if not check.ok
        return { ok: false, status: check.status, error: check.error }
    end if
    if check.data.Authenticated <> true
        return { ok: true, authenticated: false }
    end if
    auth = httpPostJson(base + "/Users/AuthenticateWithQuickConnect", "", { Secret: m.top.qcSecret })
    if not auth.ok
        return { ok: false, status: auth.status, error: auth.error }
    end if
    d = auth.data
    user = d.User
    if d.AccessToken = invalid or user = invalid
        return { ok: false, error: "Unexpected Quick Connect auth response" }
    end if
    return {
        ok: true,
        authenticated: true,
        accessToken: d.AccessToken,
        userId: user.Id,
        username: user.Name
    }
end function

function doChannels() as object
    tvBase = m.top.tvBaseUrl
    if tvBase = "" then tvBase = m.top.serverUrl
    resp = httpGetJson(tvBase + "/ClassicTV/channels", m.top.accessToken)
    if not resp.ok
        return { ok: false, status: resp.status, error: resp.error }
    end if
    channels = resp.data.Channels
    if channels = invalid then channels = []
    return { ok: true, channels: channels }
end function

function rokuDeviceProfile() as object
    return {
        MaxStreamingBitrate: 40000000,
        MaxStaticBitrate: 40000000,
        DirectPlayProfiles: [
            { Container: "mp4,m4v,mov", Type: "Video", VideoCodec: "h264,hevc", AudioCodec: "aac,mp3,ac3,eac3" },
            { Container: "mkv", Type: "Video", VideoCodec: "h264,hevc", AudioCodec: "aac,mp3,ac3,eac3" }
        ],
        TranscodingProfiles: [
            {
                Container: "ts",
                Type: "Video",
                VideoCodec: "h264",
                AudioCodec: "aac,mp3",
                Protocol: "hls",
                Context: "Streaming",
                MaxAudioChannels: "2",
                MinSegments: 1,
                BreakOnNonKeyFrames: true
            }
        ],
        SubtitleProfiles: [
            { Format: "srt", Method: "External" },
            { Format: "vtt", Method: "External" }
        ]
    }
end function

function doTune() as object
    tvBase = m.top.tvBaseUrl
    if tvBase = "" then tvBase = m.top.serverUrl
    chId = m.top.channelId
    esc = CreateObject("roUrlTransfer").Escape(chId)
    slotResp = httpGetJson(tvBase + "/ClassicTV/channel/" + esc, m.top.accessToken)
    if not slotResp.ok
        return { ok: false, status: slotResp.status, error: slotResp.error }
    end if
    slot = slotResp.data
    itemId = slot.JellyfinItemId
    if itemId = invalid or itemId = ""
        return { ok: false, error: "No item scheduled" }
    end if

    offset = 0.0
    if slot.StartOffset <> invalid then offset = slot.StartOffset

    ' Channel 1 pre-rendered TV Guide MP4
    if Left(itemId, 6) = "guide-"
        return {
            ok: true,
            slot: slot,
            streamUrl: tvBase + "/ClassicTV/guide/stream/" + itemId,
            streamFormat: "mp4",
            offset: offset,
            subtitleUrl: "",
            authHeader: authHeader(m.top.accessToken)
        }
    end if

    serverUrl = m.top.serverUrl
    token = m.top.accessToken
    userId = m.top.userId
    pbUrl = serverUrl + "/Items/" + itemId + "/PlaybackInfo?UserId=" + userId + "&IsPlayback=true&AutoOpenLiveStream=true"
    pbResp = httpPostJson(pbUrl, token, {
        DeviceProfile: rokuDeviceProfile(),
        UserId: userId,
        IsPlayback: true,
        AutoOpenLiveStream: true,
        MaxStreamingBitrate: 40000000
    })
    if not pbResp.ok
        return { ok: false, status: pbResp.status, error: "PlaybackInfo " + pbResp.error }
    end if

    sources = pbResp.data.MediaSources
    if sources = invalid or sources.Count() = 0
        return { ok: false, error: "No media source" }
    end if
    src = sources[0]

    streamUrl = ""
    streamFormat = "hls"
    if src.SupportsDirectPlay = true
        streamUrl = serverUrl + "/Videos/" + itemId + "/stream?static=true&MediaSourceId=" + src.Id + "&api_key=" + token
        if src.Container = "mkv"
            streamFormat = "mkv"
        else
            streamFormat = "mp4"
        end if
    else if src.TranscodingUrl <> invalid and src.TranscodingUrl <> ""
        tUrl = src.TranscodingUrl
        if Left(tUrl, 4) = "http"
            streamUrl = tUrl
        else
            streamUrl = serverUrl + tUrl
        end if
        streamFormat = "hls"
    else
        streamUrl = serverUrl + "/Videos/" + itemId + "/master.m3u8?MediaSourceId=" + src.Id + "&VideoCodec=h264&AudioCodec=aac&VideoBitrate=12000000&AudioBitrate=320000&api_key=" + token
        streamFormat = "hls"
    end if

    subUrl = ""
    if src.MediaStreams <> invalid
        for each st in src.MediaStreams
            if st.Type = "Subtitle"
                subUrl = serverUrl + "/Videos/" + itemId + "/" + src.Id + "/Subtitles/" + st.Index.ToStr() + "/Stream.srt?api_key=" + token
                exit for
            end if
        end for
    end if

    return {
        ok: true,
        slot: slot,
        streamUrl: streamUrl,
        streamFormat: streamFormat,
        offset: offset,
        subtitleUrl: subUrl
    }
end function
