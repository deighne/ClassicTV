import AVFoundation
import Combine
import Foundation

@MainActor
final class PlayerViewModel: ObservableObject {
    @Published var auth: AuthSession?
    @Published var channels: [ChannelInfo] = []
    @Published var currentIndex: Int = 0
    @Published var currentSlot: ChannelSlot?
    @Published var nowPlaying: [String: String] = [:]

    @Published var showStatic: Bool = false
    @Published var bugVisible: Bool = false
    @Published var bugNumber: String = ""
    @Published var bugName: String = ""
    @Published var bugTitle: String = ""
    @Published var isMuted: Bool = false
    @Published var digitDisplay: String = ""
    @Published var loginError: String = ""
    @Published var isLoggingIn: Bool = false
    @Published var quickConnectCode: String?

    let player = AVPlayer()

    private let client = ClassicTVClient()
    private let authKey = "classictv_ios_auth_v1"
    private let lastChannelKey = "classictv_ios_last_channel_v1"
    private let deviceIdKey = "classictv_ios_device_id_v1"

    private var tuneGeneration: Int = 0
    private var staticShownAt: Date = .distantPast
    private var staticHideTask: Task<Void, Never>?
    private var bugHideTask: Task<Void, Never>?
    private var mirrorTask: Task<Void, Never>?
    private var quickConnectTask: Task<Void, Never>?
    private var digitBuffer: String = ""
    private var digitTask: Task<Void, Never>?
    private var itemStatusObs: NSKeyValueObservation?
    private var endObserver: NSObjectProtocol?

    init() {
        if let raw = UserDefaults.standard.data(forKey: authKey),
           let stored = try? JSONDecoder().decode(AuthSession.self, from: raw) {
            self.auth = stored
            Task { await loadChannelsAndTune() }
        }
    }

    private func deviceId() -> String {
        if let existing = UserDefaults.standard.string(forKey: deviceIdKey), !existing.isEmpty {
            return existing
        }
        let fresh = "ctv-ios-\(UUID().uuidString.prefix(8))"
        UserDefaults.standard.set(fresh, forKey: deviceIdKey)
        return fresh
    }

    func signIn(serverURL: String, username: String, password: String) async {
        loginError = ""
        isLoggingIn = true
        defer { isLoggingIn = false }
        do {
            let session = try await client.authenticate(
                serverURL: serverURL,
                username: username,
                password: password,
                deviceId: deviceId()
            )
            if let encoded = try? JSONEncoder().encode(session) {
                UserDefaults.standard.set(encoded, forKey: authKey)
            }
            self.auth = session
            await loadChannelsAndTune()
        } catch {
            loginError = error.localizedDescription
        }
    }

    func startQuickConnect(serverURL: String) {
        cancelQuickConnect()
        loginError = ""
        let devId = deviceId()
        quickConnectTask = Task { [weak self] in
            guard let vm = self else { return }
            do {
                let initRes = try await vm.client.initiateQuickConnect(serverURL: serverURL, deviceId: devId)
                guard !Task.isCancelled else { return }
                vm.quickConnectCode = initRes.code
                for _ in 0..<150 {
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                    guard !Task.isCancelled else { return }
                    let check = try await vm.client.checkQuickConnect(serverURL: serverURL, secret: initRes.secret, deviceId: devId)
                    if check.authenticated {
                        let session = try await vm.client.authenticateWithQuickConnect(serverURL: serverURL, secret: initRes.secret, deviceId: devId)
                        if let encoded = try? JSONEncoder().encode(session) {
                            UserDefaults.standard.set(encoded, forKey: vm.authKey)
                        }
                        vm.quickConnectCode = nil
                        vm.auth = session
                        await vm.loadChannelsAndTune()
                        return
                    }
                }
                vm.quickConnectCode = nil
                vm.loginError = "Quick Connect timed out."
            } catch is CancellationError {
                vm.quickConnectCode = nil
            } catch {
                vm.quickConnectCode = nil
                vm.loginError = error.localizedDescription
            }
        }
    }

    func cancelQuickConnect() {
        quickConnectTask?.cancel()
        quickConnectTask = nil
        quickConnectCode = nil
    }

    func signOut() {
        tuneGeneration += 1
        mirrorTask?.cancel()
        digitTask?.cancel()
        itemStatusObs?.invalidate()
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
            self.endObserver = nil
        }
        player.pause()
        player.replaceCurrentItem(with: nil)
        UserDefaults.standard.removeObject(forKey: authKey)
        auth = nil
        channels = []
        currentSlot = nil
        nowPlaying = [:]
        showStatic = false
        bugVisible = false
    }

    func channelNumber(for ch: ChannelInfo, index: Int) -> Int {
        ch.number > 0 ? ch.number : index + 1
    }

    func loadChannelsAndTune() async {
        guard let auth else { return }
        do {
            let list = try await client.fetchChannels(auth: auth)
            self.channels = list
            guard !list.isEmpty else {
                presentBug(number: "--", name: "Classic TV", title: "No channels enabled")
                return
            }
            let lastId = UserDefaults.standard.string(forKey: lastChannelKey)
            let startIdx = list.firstIndex(where: { $0.id == lastId }) ?? 0
            tune(index: startIdx)
            Task { await refreshNowPlaying() }
        } catch {
            presentBug(number: "ERR", name: "Classic TV", title: error.localizedDescription)
        }
    }

    func refreshNowPlaying() async {
        guard let auth else { return }
        let list = channels
        await withTaskGroup(of: (String, String)?.self) { group in
            for ch in list {
                group.addTask { [client] in
                    guard let slot = try? await client.fetchSlot(auth: auth, channelId: ch.id) else {
                        return nil
                    }
                    let label: String
                    if let ep = slot.episodeTitle, !ep.isEmpty, ep != slot.title {
                        label = "\(slot.title) — \(ep)"
                    } else {
                        label = slot.title
                    }
                    return (ch.id, label)
                }
            }
            for await pair in group {
                if let (id, label) = pair {
                    nowPlaying[id] = label
                }
            }
        }
    }

    func stepChannel(_ delta: Int) {
        guard !channels.isEmpty else { return }
        let next = (currentIndex + delta + channels.count) % channels.count
        tune(index: next)
    }

    func tuneGuide() {
        if let idx = channels.enumerated().first(where: { channelNumber(for: $0.element, index: $0.offset) == 1 || $0.element.id == "guide" })?.offset {
            tune(index: idx)
        }
    }

    func enterDigit(_ digit: String) {
        digitTask?.cancel()
        if digitBuffer.count < 4 {
            digitBuffer += digit
        }
        digitDisplay = "CH \(digitBuffer)_"
        presentBug(number: "\(digitBuffer)_", name: "Direct Tune", title: "", duration: 2.0)
        digitTask = Task {
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            guard !Task.isCancelled else { return }
            commitDigits()
        }
    }

    func clearDigits() {
        digitTask?.cancel()
        digitBuffer = ""
        digitDisplay = ""
    }

    func commitDigits() {
        digitTask?.cancel()
        guard !digitBuffer.isEmpty else { return }
        let target = Int(digitBuffer)
        digitBuffer = ""
        guard let target else {
            digitDisplay = ""
            return
        }
        if let idx = channels.enumerated().first(where: { channelNumber(for: $0.element, index: $0.offset) == target })?.offset {
            digitDisplay = "CH \(target)"
            tune(index: idx)
        } else {
            digitDisplay = "CH \(target) — Not Found"
            presentBug(number: "\(target)", name: "Not Found", title: "", duration: 2.0)
        }
    }

    func toggleMute() {
        isMuted.toggle()
        player.isMuted = isMuted
    }

    func tune(index: Int) {
        guard !channels.isEmpty, let auth else { return }
        let clamped = (index + channels.count) % channels.count
        currentIndex = clamped
        let ch = channels[clamped]
        let chNum = channelNumber(for: ch, index: clamped)
        UserDefaults.standard.set(ch.id, forKey: lastChannelKey)

        tuneGeneration += 1
        let gen = tuneGeneration

        mirrorTask?.cancel()
        itemStatusObs?.invalidate()
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
            self.endObserver = nil
        }

        player.pause()
        player.replaceCurrentItem(with: nil)

        beginStatic()
        presentBug(number: "\(chNum)", name: ch.name, title: "Tuning…")

        Task { [weak self] in
            guard let vm = self else { return }
            do {
                let slot = try await vm.client.fetchSlot(auth: auth, channelId: ch.id)
                guard gen == vm.tuneGeneration else { return }
                vm.currentSlot = slot

                let displayTitle: String
                if let ep = slot.episodeTitle, !ep.isEmpty, ep != slot.title {
                    displayTitle = "\(slot.title) — \(ep)"
                } else {
                    displayTitle = slot.title
                }
                vm.nowPlaying[ch.id] = displayTitle
                vm.presentBug(number: "\(chNum)", name: ch.name, title: displayTitle)

                let streamURL = try await vm.client.resolveStreamURL(auth: auth, slot: slot)
                guard gen == vm.tuneGeneration else { return }

                let asset = AVURLAsset(
                    url: streamURL,
                    options: ["AVURLAssetHTTPHeaderFieldsKey": vm.client.streamHeaders(auth: auth)]
                )
                let item = AVPlayerItem(asset: asset)
                let offset = slot.startOffset ?? 0

                vm.itemStatusObs = item.observe(\.status, options: [.new]) { [weak self] observed, _ in
                    guard let self else { return }
                    Task { @MainActor in
                        guard gen == self.tuneGeneration else { return }
                        if observed.status == .readyToPlay {
                            if offset > 0 {
                                let target = CMTime(seconds: offset, preferredTimescale: 600)
                                await self.player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
                            }
                            self.player.isMuted = self.isMuted
                            self.player.play()
                            self.endStatic()
                        } else if observed.status == .failed {
                            self.presentBug(number: "\(chNum)", name: ch.name, title: observed.error?.localizedDescription ?? "Playback failed")
                            self.endStatic(immediate: true)
                            try? await Task.sleep(nanoseconds: 2_500_000_000)
                            if gen == self.tuneGeneration {
                                self.tune(index: self.currentIndex)
                            }
                        }
                    }
                }

                vm.endObserver = NotificationCenter.default.addObserver(
                    forName: .AVPlayerItemDidPlayToEndTime,
                    object: item,
                    queue: .main
                ) { [weak self] _ in
                    guard let self else { return }
                    Task { @MainActor in
                        guard gen == self.tuneGeneration else { return }
                        self.tune(index: self.currentIndex)
                    }
                }

                vm.player.replaceCurrentItem(with: item)

                if ch.mirror {
                    vm.startMirrorPolling(channel: ch, generation: gen)
                }
            } catch ClassicTVError.unauthorized {
                guard gen == vm.tuneGeneration else { return }
                vm.signOut()
            } catch {
                guard gen == vm.tuneGeneration else { return }
                vm.presentBug(number: "\(chNum)", name: ch.name, title: error.localizedDescription)
                vm.endStatic(immediate: true)
            }
        }
    }

    private func startMirrorPolling(channel: ChannelInfo, generation: Int) {
        mirrorTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard !Task.isCancelled, generation == tuneGeneration, let auth else { return }
                guard let latest = try? await client.fetchSlot(auth: auth, channelId: channel.id) else {
                    continue
                }
                if latest.jellyfinItemId != currentSlot?.jellyfinItemId {
                    tune(index: currentIndex)
                    return
                }
                if latest.paused == true {
                    player.pause()
                } else {
                    if player.timeControlStatus != .playing {
                        player.play()
                    }
                    if let target = latest.startOffset {
                        let cur = player.currentTime().seconds
                        if cur.isFinite && abs(cur - target) > 4.0 {
                            await player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
                        }
                    }
                }
            }
        }
    }

    func showCurrentBug() {
        guard !channels.isEmpty else { return }
        let ch = channels[currentIndex]
        let num = channelNumber(for: ch, index: currentIndex)
        let title: String
        if let slot = currentSlot {
            if let ep = slot.episodeTitle, !ep.isEmpty, ep != slot.title {
                title = "\(slot.title) — \(ep)"
            } else {
                title = slot.title
            }
        } else {
            title = ch.name
        }
        presentBug(number: "\(num)", name: ch.name, title: title)
    }

    private func beginStatic() {
        staticHideTask?.cancel()
        staticShownAt = Date()
        showStatic = true
    }

    private func endStatic(immediate: Bool = false) {
        staticHideTask?.cancel()
        if immediate {
            showStatic = false
            return
        }
        let elapsed = Date().timeIntervalSince(staticShownAt)
        let remaining = max(0.22, 0.55 - elapsed)
        staticHideTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            guard !Task.isCancelled else { return }
            showStatic = false
        }
    }

    private func presentBug(number: String, name: String, title: String, duration: Double = 8.0) {
        bugNumber = number
        bugName = name
        bugTitle = title
        bugVisible = true
        bugHideTask?.cancel()
        guard duration > 0 else { return }
        bugHideTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
            guard !Task.isCancelled else { return }
            bugVisible = false
        }
    }
}
