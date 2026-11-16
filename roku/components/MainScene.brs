sub init()
    m.video = m.top.findNode("tvVideo")
    m.staticPoster = m.top.findNode("staticPoster")
    m.channelBug = m.top.findNode("channelBug")
    m.bugNumber = m.top.findNode("bugNumber")
    m.bugName = m.top.findNode("bugName")
    m.bugTitle = m.top.findNode("bugTitle")
    m.drawerGroup = m.top.findNode("drawerGroup")
    m.channelList = m.top.findNode("channelList")

    m.staticTickTimer = m.top.findNode("staticTickTimer")
    m.staticHideTimer = m.top.findNode("staticHideTimer")
    m.bugHideTimer = m.top.findNode("bugHideTimer")
    m.digitTimer = m.top.findNode("digitTimer")
    m.errorRetryTimer = m.top.findNode("errorRetryTimer")
    m.qcPollTimer = m.top.findNode("qcPollTimer")

    m.video.observeField("state", "onVideoStateChanged")
    m.channelList.observeField("itemSelected", "onDrawerItemSelected")
    m.staticTickTimer.observeField("fire", "onStaticTick")
    m.staticHideTimer.observeField("fire", "onStaticHideFire")
    m.bugHideTimer.observeField("fire", "onBugHideFire")
    m.digitTimer.observeField("fire", "onDigitCommit")
    m.errorRetryTimer.observeField("fire", "onErrorRetryFire")
    m.qcPollTimer.observeField("fire", "onQcPollFire")

    m.channels = []
    m.currentIndex = 0
    m.tuneSeq = 0
    m.staticFrame = 0
    m.staticShownAt = 0
    m.digitBuffer = ""
    m.pendingSubUrl = ""
    m.ccEnabled = false

    m.reg = CreateObject("roRegistrySection", "ClassicTV")
    m.serverUrl = m.reg.Read("serverUrl")
    if m.serverUrl = "" then m.serverUrl = "https://jellyfin.example.com"
    m.tvBaseUrl = m.serverUrl
    m.accessToken = m.reg.Read("accessToken")
    m.userId = m.reg.Read("userId")
    m.deviceId = m.reg.Read("deviceId")
    if m.deviceId = ""
        m.deviceId = "ctv-roku-" + CreateObject("roDeviceInfo").GetChannelClientId()
        m.reg.Write("deviceId", m.deviceId)
        m.reg.Flush()
    end if

    m.top.setFocus(true)

    if m.accessToken <> "" and m.userId <> ""
        loadChannels()
    else
        promptLoginUsername()
    end if
end sub

' --- Login flow via KeyboardDialog ---

sub promptLoginUsername()
    dlg = CreateObject("roSGNode", "KeyboardDialog")
    dlg.title = "Classic TV — Jellyfin Username"
    dlg.buttons = ["Next", "Quick Connect", "Server URL"]
    dlg.observeField("buttonSelected", "onUserDlgButton")
    m.loginDlg = dlg
    m.top.dialog = dlg
end sub

sub onUserDlgButton()
    btn = m.loginDlg.buttonSelected
    text = m.loginDlg.text
    m.top.dialog = invalid
    if btn = 1
        startQuickConnect()
        return
    else if btn = 2
        promptServerUrl()
        return
    end if
    m.loginUsername = text.Trim()
    if m.loginUsername = ""
        promptLoginUsername()
        return
    end if
    promptLoginPassword()
end sub

sub startQuickConnect()
    task = CreateObject("roSGNode", "ApiTask")
    task.action = "qc_init"
    task.serverUrl = m.serverUrl
    task.deviceId = m.deviceId
    task.observeField("result", "onQcInitResult")
    m.qcInitTask = task
    task.control = "RUN"
end sub

sub onQcInitResult()
    res = m.qcInitTask.result
    if res = invalid or res.ok <> true
        errMsg = "Quick Connect failed."
        if res <> invalid and res.error <> invalid then errMsg = res.error
        dlg = CreateObject("roSGNode", "Dialog")
        dlg.title = "Quick Connect"
        dlg.message = errMsg
        dlg.buttons = ["Back"]
        dlg.observeField("buttonSelected", "onQcCancel")
        m.top.dialog = dlg
        return
    end if

    m.qcSecret = res.secret
    dlg = CreateObject("roSGNode", "Dialog")
    dlg.title = "Jellyfin Quick Connect"
    dlg.message = "Enter code in Jellyfin -> Quick Connect:" + Chr(10) + Chr(10) + res.code
    dlg.buttons = ["Cancel"]
    dlg.observeField("buttonSelected", "onQcCancel")
    m.qcDlg = dlg
    m.top.dialog = dlg
    m.qcPollTimer.control = "start"
end sub

sub onQcCancel()
    m.qcPollTimer.control = "stop"
    m.qcSecret = ""
    m.top.dialog = invalid
    promptLoginUsername()
end sub

sub onQcPollFire()
    if m.qcSecret = invalid or m.qcSecret = "" then return
    task = CreateObject("roSGNode", "ApiTask")
    task.action = "qc_poll"
    task.serverUrl = m.serverUrl
    task.deviceId = m.deviceId
    task.qcSecret = m.qcSecret
    task.observeField("result", "onQcPollResult")
    m.qcPollTask = task
    task.control = "RUN"
end sub

sub onQcPollResult()
    res = m.qcPollTask.result
    if res = invalid or res.ok <> true then return
    if res.authenticated = true
        m.qcPollTimer.control = "stop"
        m.qcSecret = ""
        m.top.dialog = invalid
        m.accessToken = res.accessToken
        m.userId = res.userId
        m.reg.Write("accessToken", m.accessToken)
        m.reg.Write("userId", m.userId)
        m.reg.Flush()
        loadChannels()
    end if
end sub

sub promptServerUrl()
    dlg = CreateObject("roSGNode", "KeyboardDialog")
    dlg.title = "Jellyfin Server URL"
    dlg.text = m.serverUrl
    dlg.buttons = ["Save"]
    dlg.observeField("buttonSelected", "onServerDlgButton")
    m.serverDlg = dlg
    m.top.dialog = dlg
end sub

sub onServerDlgButton()
    u = m.serverDlg.text.Trim()
    m.top.dialog = invalid
    if u <> ""
        while Right(u, 1) = "/"
            u = Left(u, Len(u) - 1)
        end while
        m.serverUrl = u
        m.tvBaseUrl = u
        m.reg.Write("serverUrl", m.serverUrl)
        m.reg.Write("tvBaseUrl", m.tvBaseUrl)
        m.reg.Flush()
    end if
    promptLoginUsername()
end sub

sub promptLoginPassword()
    dlg = CreateObject("roSGNode", "KeyboardDialog")
    dlg.title = "Password for " + m.loginUsername
    dlg.keyboard.textEditBox.secureMode = true
    dlg.buttons = ["Sign In", "Back"]
    dlg.observeField("buttonSelected", "onPassDlgButton")
    m.passDlg = dlg
    m.top.dialog = dlg
end sub

sub onPassDlgButton()
    btn = m.passDlg.buttonSelected
    pw = m.passDlg.text
    m.top.dialog = invalid
    if btn = 1
        promptLoginUsername()
        return
    end if

    task = CreateObject("roSGNode", "ApiTask")
    task.action = "login"
    task.serverUrl = m.serverUrl
    task.deviceId = m.deviceId
    task.username = m.loginUsername
    task.password = pw
    task.observeField("result", "onLoginResult")
    m.loginTask = task
    task.control = "RUN"
end sub

sub onLoginResult()
    res = m.loginTask.result
    if res <> invalid and res.ok = true
        m.accessToken = res.accessToken
        m.userId = res.userId
        m.reg.Write("accessToken", m.accessToken)
        m.reg.Write("userId", m.userId)
        m.reg.Flush()
        loadChannels()
    else
        errMsg = "Sign-in failed."
        if res <> invalid and res.error <> invalid then errMsg = res.error
        dlg = CreateObject("roSGNode", "Dialog")
        dlg.title = "Classic TV"
        dlg.message = errMsg
        dlg.buttons = ["Try Again"]
        dlg.observeField("buttonSelected", "onLoginErrClose")
        m.top.dialog = dlg
    end if
end sub

sub onLoginErrClose()
    m.top.dialog = invalid
    promptLoginUsername()
end sub

sub promptSignOut()
    dlg = CreateObject("roSGNode", "Dialog")
    dlg.title = "Classic TV"
    dlg.message = "Sign out of Jellyfin?"
    dlg.buttons = ["Sign Out", "Cancel"]
    dlg.observeField("buttonSelected", "onSignOutDlgButton")
    m.signOutDlg = dlg
    m.top.dialog = dlg
end sub

sub onSignOutDlgButton()
    btn = m.signOutDlg.buttonSelected
    m.top.dialog = invalid
    if btn = 0
        signOut()
    end if
end sub

sub signOut()
    m.video.control = "stop"
    m.accessToken = ""
    m.userId = ""
    m.reg.Delete("accessToken")
    m.reg.Delete("userId")
    m.reg.Flush()
    promptLoginUsername()
end sub

' --- Channels & Tuning ---

sub loadChannels()
    task = CreateObject("roSGNode", "ApiTask")
    task.action = "channels"
    task.serverUrl = m.serverUrl
    task.tvBaseUrl = m.tvBaseUrl
    task.accessToken = m.accessToken
    task.observeField("result", "onChannelsResult")
    m.channelsTask = task
    task.control = "RUN"
end sub

sub onChannelsResult()
    res = m.channelsTask.result
    if res = invalid or res.ok <> true
        showBug("ERR", "Classic TV", "Failed to load channels")
        return
    end if

    raw = res.channels
    ' Sort by Number (0 = unassigned -> 9999)
    sorted = []
    for each ch in raw
        num = 9999
        if ch.Number <> invalid and ch.Number > 0 then num = ch.Number
        ch._sortNum = num
        sorted.Push(ch)
    end for
    sorted.SortBy("_sortNum")
    m.channels = sorted

    ' Populate drawer list
    content = CreateObject("roSGNode", "ContentNode")
    for i = 0 to m.channels.Count() - 1
        ch = m.channels[i]
        chNum = getChannelNumber(ch, i)
        item = content.CreateChild("ContentNode")
        item.title = chNum.ToStr() + "   " + ch.Name
    end for
    m.channelList.content = content

    if m.channels.Count() = 0
        showBug("--", "Classic TV", "No channels enabled")
        return
    end if

    lastId = m.reg.Read("lastChannel")
    startIdx = 0
    if lastId <> ""
        for i = 0 to m.channels.Count() - 1
            if m.channels[i].Id = lastId
                startIdx = i
                exit for
            end if
        end for
    end if
    tuneIndex(startIdx)
end sub

function getChannelNumber(ch as object, idx as integer) as integer
    if ch.Number <> invalid and ch.Number > 0 then return ch.Number
    return idx + 1
end function

sub tuneIndex(idx as integer)
    if m.channels.Count() = 0 then return
    m.errorRetryTimer.control = "stop"
    m.tuneSeq = m.tuneSeq + 1
    m.currentIndex = (idx + m.channels.Count()) mod m.channels.Count()
    ch = m.channels[m.currentIndex]
    chNum = getChannelNumber(ch, m.currentIndex)

    m.reg.Write("lastChannel", ch.Id)
    m.reg.Flush()

    m.video.control = "stop"
    showStatic()
    showBug(chNum.ToStr(), ch.Name, "Tuning...")

    task = CreateObject("roSGNode", "ApiTask")
    task.action = "tune"
    task.serverUrl = m.serverUrl
    task.tvBaseUrl = m.tvBaseUrl
    task.accessToken = m.accessToken
    task.userId = m.userId
    task.deviceId = m.deviceId
    task.channelId = ch.Id
    task.observeField("result", "onTuneResult")
    m.tuneTask = task
    m.tuneTaskSeq = m.tuneSeq
    task.control = "RUN"
end sub

sub onTuneResult()
    if m.tuneTaskSeq <> m.tuneSeq then return
    res = m.tuneTask.result
    ch = m.channels[m.currentIndex]
    chNum = getChannelNumber(ch, m.currentIndex)

    if res = invalid or res.ok <> true
        errMsg = "Channel offline"
        if res <> invalid and res.error <> invalid then errMsg = res.error
        if res <> invalid and res.status = 401
            signOut()
            return
        end if
        showBug(chNum.ToStr(), ch.Name, errMsg)
        hideStatic(true)
        return
    end if

    slot = res.slot
    title = ch.Name
    if slot.Title <> invalid and slot.Title <> "" then title = slot.Title
    if slot.EpisodeTitle <> invalid and slot.EpisodeTitle <> "" and slot.EpisodeTitle <> title
        title = title + " - " + slot.EpisodeTitle
    end if
    showBug(chNum.ToStr(), ch.Name, title)

    content = CreateObject("roSGNode", "ContentNode")
    content.url = res.streamUrl
    content.streamFormat = res.streamFormat
    content.title = title
    if res.authHeader <> invalid and res.authHeader <> ""
        content.HttpHeaders = ["Authorization: " + res.authHeader]
        content.HttpCertificatesFile = "common:/certs/ca-bundle.crt"
    end if
    if res.offset <> invalid and res.offset > 0
        content.playStart = Int(res.offset)
    end if
    if res.subtitleUrl <> invalid and res.subtitleUrl <> ""
        content.SubtitleConfig = {
            TrackName: res.subtitleUrl
        }
    end if

    m.video.content = content
    m.video.control = "play"
end sub

sub onVideoStateChanged()
    st = m.video.state
    if st = "playing"
        m.errorRetryTimer.control = "stop"
        hideStatic(false)
    else if st = "finished"
        tuneIndex(m.currentIndex)
    else if st = "error"
        m.errorRetryTimer.control = "stop"
        m.errorRetryTimer.control = "start"
    end if
end sub

sub onErrorRetryFire()
    if m.channels.Count() > 0
        tuneIndex(m.currentIndex)
    end if
end sub

' --- CRT Static & Trinitron Bug ---

sub showStatic()
    m.staticHideTimer.control = "stop"
    dt = CreateObject("roDateTime")
    m.staticShownAt = dt.AsSeconds()
    m.staticPoster.visible = true
    m.staticTickTimer.control = "start"
end sub

sub onStaticTick()
    m.staticFrame = (m.staticFrame + 1) mod 4
    if m.staticFrame = 0
        m.staticPoster.scale = [1.0, 1.0]
    else if m.staticFrame = 1
        m.staticPoster.scale = [-1.0, 1.0]
    else if m.staticFrame = 2
        m.staticPoster.scale = [1.0, -1.0]
    else
        m.staticPoster.scale = [-1.0, -1.0]
    end if
end sub

sub hideStatic(immediate as boolean)
    if not m.staticPoster.visible then return
    if immediate
        m.staticTickTimer.control = "stop"
        m.staticHideTimer.control = "stop"
        m.staticPoster.visible = false
        return
    end if
    dt = CreateObject("roDateTime")
    elapsed = dt.AsSeconds() - m.staticShownAt
    if elapsed >= 1
        onStaticHideFire()
    else
        m.staticHideTimer.control = "start"
    end if
end sub

sub onStaticHideFire()
    m.staticTickTimer.control = "stop"
    m.staticPoster.visible = false
end sub

sub showBug(numText as string, nameText as string, titleText as string)
    m.bugNumber.text = numText
    m.bugName.text = UCase(nameText)
    m.bugTitle.text = titleText
    m.channelBug.visible = true
    m.bugHideTimer.control = "stop"
    m.bugHideTimer.control = "start"
end sub

sub onBugHideFire()
    m.channelBug.visible = false
end sub

' --- Drawer & Digits ---

sub onDrawerItemSelected()
    idx = m.channelList.itemSelected
    m.drawerGroup.visible = false
    m.top.setFocus(true)
    tuneIndex(idx)
end sub

sub onDigitCommit()
    if m.digitBuffer = "" then return
    target = Val(m.digitBuffer)
    m.digitBuffer = ""
    for i = 0 to m.channels.Count() - 1
        if getChannelNumber(m.channels[i], i) = target
            tuneIndex(i)
            return
        end if
    end for
    showBug(target.ToStr(), "Not Found", "")
end sub

function onKeyEvent(key as string, press as boolean) as boolean
    if not press then return false

    if m.drawerGroup.visible
        if key = "back" or key = "right"
            m.drawerGroup.visible = false
            m.top.setFocus(true)
            return true
        end if
        return false
    end if

    if key = "up" or key = "channelup"
        tuneIndex(m.currentIndex + 1)
        return true
    else if key = "down" or key = "channeldown"
        tuneIndex(m.currentIndex - 1)
        return true
    else if key = "OK" or key = "select" or key = "left"
        m.drawerGroup.visible = true
        m.channelList.jumpToItem = m.currentIndex
        m.channelList.setFocus(true)
        return true
    else if key = "replay"
        for i = 0 to m.channels.Count() - 1
            if getChannelNumber(m.channels[i], i) = 1 or m.channels[i].Id = "guide"
                tuneIndex(i)
                return true
            end if
        end for
        return true
    else if key = "options"
        promptSignOut()
        return true
    else if key = "info"
        if m.channels.Count() > 0
            ch = m.channels[m.currentIndex]
            showBug(getChannelNumber(ch, m.currentIndex).ToStr(), ch.Name, "")
        end if
        return true
    else if Len(key) = 1 and key >= "0" and key <= "9"
        m.digitBuffer = m.digitBuffer + key
        showBug(m.digitBuffer + "_", "Channel Entry", "")
        m.digitTimer.control = "stop"
        m.digitTimer.control = "start"
        return true
    end if

    return false
end function
