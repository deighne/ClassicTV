import Foundation

enum ClassicTVError: LocalizedError {
    case invalidURL
    case unauthorized
    case httpError(Int)
    case noMediaSource

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid server URL."
        case .unauthorized: return "Session expired or invalid credentials."
        case .httpError(let code): return "Server returned HTTP \(code)."
        case .noMediaSource: return "No playable media source returned."
        }
    }
}

final class ClassicTVClient {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    private func authHeader(deviceId: String, token: String? = nil) -> String {
        let base = "MediaBrowser Client=\"ClassicTV iOS\", Device=\"iPhone\", DeviceId=\"\(deviceId)\", Version=\"1.0.0\""
        if let token, !token.isEmpty {
            return "\(base), Token=\"\(token)\""
        }
        return base
    }

    func authenticate(serverURL: String, username: String, password: String, deviceId: String) async throws -> AuthSession {
        let clean = serverURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/Users/AuthenticateByName") else {
            throw ClassicTVError.invalidURL
        }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(authHeader(deviceId: deviceId), forHTTPHeaderField: "Authorization")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["Username": username, "Pw": password])

        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw ClassicTVError.unauthorized }
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }

        let decoded = try JSONDecoder().decode(AuthenticateResponse.self, from: data)
        return AuthSession(
            serverURL: clean,
            tvBaseURL: clean,
            accessToken: decoded.accessToken,
            userId: decoded.user.id,
            username: decoded.user.name,
            deviceId: deviceId
        )
    }

    func initiateQuickConnect(serverURL: String, deviceId: String) async throws -> QuickConnectResult {
        let clean = serverURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/QuickConnect/Initiate") else {
            throw ClassicTVError.invalidURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(authHeader(deviceId: deviceId), forHTTPHeaderField: "Authorization")
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }
        return try JSONDecoder().decode(QuickConnectResult.self, from: data)
    }

    func checkQuickConnect(serverURL: String, secret: String, deviceId: String) async throws -> QuickConnectResult {
        let clean = serverURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let escaped = secret.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
              let url = URL(string: "\(clean)/QuickConnect/Connect?secret=\(escaped)") else {
            throw ClassicTVError.invalidURL
        }
        var req = URLRequest(url: url)
        req.setValue(authHeader(deviceId: deviceId), forHTTPHeaderField: "Authorization")
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }
        return try JSONDecoder().decode(QuickConnectResult.self, from: data)
    }

    func authenticateWithQuickConnect(serverURL: String, secret: String, deviceId: String) async throws -> AuthSession {
        let clean = serverURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/Users/AuthenticateWithQuickConnect") else {
            throw ClassicTVError.invalidURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(authHeader(deviceId: deviceId), forHTTPHeaderField: "Authorization")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["Secret": secret])
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw ClassicTVError.unauthorized }
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }
        let decoded = try JSONDecoder().decode(AuthenticateResponse.self, from: data)
        return AuthSession(
            serverURL: clean,
            tvBaseURL: clean,
            accessToken: decoded.accessToken,
            userId: decoded.user.id,
            username: decoded.user.name,
            deviceId: deviceId
        )
    }

    func fetchChannels(auth: AuthSession) async throws -> [ChannelInfo] {
        guard let url = URL(string: "\(auth.tvBaseURL)/ClassicTV/channels") else {
            throw ClassicTVError.invalidURL
        }
        var req = URLRequest(url: url)
        req.setValue(authHeader(deviceId: auth.deviceId, token: auth.accessToken), forHTTPHeaderField: "Authorization")
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw ClassicTVError.unauthorized }
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }
        let decoded = try JSONDecoder().decode(ChannelsResponse.self, from: data)
        return decoded.channels.sorted { a, b in
            let na = a.number > 0 ? a.number : 9999
            let nb = b.number > 0 ? b.number : 9999
            return na < nb
        }
    }

    func fetchSlot(auth: AuthSession, channelId: String) async throws -> ChannelSlot {
        guard let escaped = channelId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "\(auth.tvBaseURL)/ClassicTV/channel/\(escaped)") else {
            throw ClassicTVError.invalidURL
        }
        var req = URLRequest(url: url)
        req.setValue(authHeader(deviceId: auth.deviceId, token: auth.accessToken), forHTTPHeaderField: "Authorization")
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw ClassicTVError.unauthorized }
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }
        return try JSONDecoder().decode(ChannelSlot.self, from: data)
    }

    func streamHeaders(auth: AuthSession) -> [String: String] {
        ["Authorization": authHeader(deviceId: auth.deviceId, token: auth.accessToken)]
    }

    func resolveStreamURL(auth: AuthSession, slot: ChannelSlot) async throws -> URL {
        guard let itemId = slot.jellyfinItemId, !itemId.isEmpty else {
            throw ClassicTVError.noMediaSource
        }

        if itemId.hasPrefix("guide-") {
            guard let escaped = itemId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
                  let url = URL(string: "\(auth.tvBaseURL)/ClassicTV/guide/stream/\(escaped)") else {
                throw ClassicTVError.invalidURL
            }
            return url
        }

        guard let pbURL = URL(string: "\(auth.serverURL)/Items/\(itemId)/PlaybackInfo?UserId=\(auth.userId)&IsPlayback=true&AutoOpenLiveStream=true") else {
            throw ClassicTVError.invalidURL
        }

        let profile: [String: Any] = [
            "MaxStreamingBitrate": 40_000_000,
            "MaxStaticBitrate": 40_000_000,
            "DirectPlayProfiles": [
                ["Container": "mp4,m4v,mov", "Type": "Video", "VideoCodec": "h264,hevc", "AudioCodec": "aac,mp3,ac3,eac3"]
            ],
            "TranscodingProfiles": [
                [
                    "Container": "ts",
                    "Type": "Video",
                    "VideoCodec": "h264,hevc",
                    "AudioCodec": "aac,mp3",
                    "Protocol": "hls",
                    "Context": "Streaming",
                    "MaxAudioChannels": "2",
                    "MinSegments": 1,
                    "BreakOnNonKeyFrames": true
                ]
            ]
        ]

        var req = URLRequest(url: pbURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(authHeader(deviceId: auth.deviceId, token: auth.accessToken), forHTTPHeaderField: "Authorization")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "DeviceProfile": profile,
            "UserId": auth.userId,
            "IsPlayback": true,
            "AutoOpenLiveStream": true,
            "MaxStreamingBitrate": 40_000_000
        ])

        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw ClassicTVError.unauthorized }
        guard (200..<300).contains(code) else { throw ClassicTVError.httpError(code) }

        let pb = try JSONDecoder().decode(PlaybackInfoResponse.self, from: data)
        guard let src = pb.mediaSources.first else {
            throw ClassicTVError.noMediaSource
        }

        if src.supportsDirectPlay == true,
           let direct = URL(string: "\(auth.serverURL)/Videos/\(itemId)/stream?static=true&MediaSourceId=\(src.id)&api_key=\(auth.accessToken)") {
            return direct
        }

        if let trans = src.transcodingUrl, !trans.isEmpty {
            let full = trans.hasPrefix("http") ? trans : "\(auth.serverURL)\(trans)"
            if let url = URL(string: full) {
                return url
            }
        }

        guard let fallback = URL(string: "\(auth.serverURL)/Videos/\(itemId)/master.m3u8?MediaSourceId=\(src.id)&VideoCodec=h264&AudioCodec=aac&VideoBitrate=12000000&AudioBitrate=320000&api_key=\(auth.accessToken)") else {
            throw ClassicTVError.invalidURL
        }
        return fallback
    }
}
