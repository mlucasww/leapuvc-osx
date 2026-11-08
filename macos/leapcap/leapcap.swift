// leapcap: AVFoundation capture helper for an unlocked Leap Motion Controller.
//
// OpenCV's macOS camera backend always asks AVFoundation for BGRA, which
// destroys the Leap's interleaved left/right bytes. This helper asks for the
// 2-byte-per-pixel YUV layouts instead and copies the bytes untouched.
//
// Commands:
//   leapcap list
//   leapcap capture --unique-id ID --out DIR [--width 640] [--height 480]
//                   [--pixel-format yuvs|2vuy] [--fps N] [--seconds 15]
//                   [--every 10] [--max-frames N]
//   leapcap stream  --unique-id ID --ring PATH [--width 640] [--height 480]
//                   [--pixel-format yuvs|2vuy] [--fps N] [--slots 4]
//                   [--seconds N]
//
// `capture` saves frames to files for diagnostics. `stream` publishes every
// frame to a shared-memory ring, and stops when stdin closes or receives
// "stop", so it cannot outlive its parent. Every line on stdout is one JSON
// object with an "event" key.
//
// Build: see ../Makefile. Camera permission belongs to the app that launches
// this binary, so run it from Terminal.app the first time.

import AVFoundation
import CoreMedia
import CoreVideo
import Foundation

// MARK: - Output

func emit(_ object: [String: Any]) {
    guard var line = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
        return
    }
    line.append(0x0A)
    FileHandle.standardOutput.write(line)
}

func fail(_ message: String, code: Int32 = 1, extra: [String: Any] = [:]) -> Never {
    var object = extra
    object["event"] = "error"
    object["message"] = message
    emit(object)
    exit(code)
}

// MARK: - Helpers

func fourCC(_ value: FourCharCode) -> String {
    let shifts: [UInt32] = [24, 16, 8, 0]
    let bytes = shifts.map { UInt8((value >> $0) & 0xFF) }
    if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }), let text = String(bytes: bytes, encoding: .ascii) {
        return text
    }
    return String(format: "0x%08x", value)
}

func authorizationName(_ status: AVAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

func ensureCameraAccess() -> AVAuthorizationStatus {
    if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
        let answered = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .video) { _ in answered.signal() }
        _ = answered.wait(timeout: .now() + 60)
    }
    return AVCaptureDevice.authorizationStatus(for: .video)
}

func discoverDevices() -> [AVCaptureDevice] {
    let types: [AVCaptureDevice.DeviceType]
    if #available(macOS 14.0, *) {
        types = [.external, .builtInWideAngleCamera]
    } else {
        types = [.externalUnknown, .builtInWideAngleCamera]
    }
    let session = AVCaptureDevice.DiscoverySession(
        deviceTypes: types,
        mediaType: .video,
        position: .unspecified
    )
    return session.devices
}

func maxRate(_ format: AVCaptureDevice.Format) -> Double {
    return format.videoSupportedFrameRateRanges.map { $0.maxFrameRate }.max() ?? 0
}

func describe(_ format: AVCaptureDevice.Format) -> [String: Any] {
    let description = format.formatDescription
    let dimensions = CMVideoFormatDescriptionGetDimensions(description)
    let ranges: [[String: Double]] = format.videoSupportedFrameRateRanges.map {
        ["min": $0.minFrameRate, "max": $0.maxFrameRate]
    }
    return [
        "subtype": fourCC(CMFormatDescriptionGetMediaSubType(description)),
        "width": Int(dimensions.width),
        "height": Int(dimensions.height),
        "frame_rates": ranges,
    ]
}

func describe(_ device: AVCaptureDevice) -> [String: Any] {
    return [
        "unique_id": device.uniqueID,
        "model_id": device.modelID,
        "name": device.localizedName,
        "manufacturer": device.manufacturer,
        "formats": device.formats.map { describe($0) },
    ]
}

func pickFormat(
    _ device: AVCaptureDevice,
    width: Int32,
    height: Int32,
    prefer: FourCharCode
) -> AVCaptureDevice.Format? {
    let sized = device.formats.filter {
        let dimensions = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
        return dimensions.width == width && dimensions.height == height
    }
    let preferred = sized.filter { CMFormatDescriptionGetMediaSubType($0.formatDescription) == prefer }
    let pool = preferred.isEmpty ? sized : preferred
    return pool.max { maxRate($0) < maxRate($1) }
}

func pixelFormat(named name: String) -> OSType? {
    switch name {
    case "yuvs": return kCVPixelFormatType_422YpCbCr8_yuvs
    case "2vuy": return kCVPixelFormatType_422YpCbCr8
    default: return nil
    }
}

/// Report everything the session and device announce, such as runtime errors.
func logNotifications(from object: AnyObject, source: String) {
    _ = NotificationCenter.default.addObserver(forName: nil, object: object, queue: nil) { note in
        emit([
            "event": "notification",
            "source": source,
            "name": note.name.rawValue,
            "info": note.userInfo.map { String(describing: $0) } ?? "",
        ])
    }
}

// MARK: - Arguments

struct Arguments {
    let values: [String]

    func string(_ name: String) -> String? {
        guard let index = values.firstIndex(of: name), index + 1 < values.count else {
            return nil
        }
        return values[index + 1]
    }

    func int(_ name: String, default fallback: Int) -> Int {
        guard let text = string(name) else { return fallback }
        guard let value = Int(text) else { fail("\(name) expects an integer, got \(text)", code: 2) }
        return value
    }

    func double(_ name: String) -> Double? {
        guard let text = string(name) else { return nil }
        guard let value = Double(text) else { fail("\(name) expects a number, got \(text)", code: 2) }
        return value
    }
}

// MARK: - Frame delivery

/// Counts frames and hands each one to a subclass on the capture queue.
class CaptureSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let maxFrames: Int
    let finished = DispatchSemaphore(value: 0)

    // These are touched only on the capture queue until the session stops.
    var seen = 0
    var dropped = 0
    var firstTime: Double?
    var lastTime: Double?
    var signalled = false

    init(maxFrames: Int) {
        self.maxFrames = maxFrames
    }

    func handle(_ sampleBuffer: CMSampleBuffer, time: Double) {}

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        seen += 1
        let time = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        if firstTime == nil { firstTime = time }
        lastTime = time

        handle(sampleBuffer, time: time)

        if maxFrames > 0 && seen >= maxFrames && !signalled {
            signalled = true
            finished.signal()
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didDrop sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        dropped += 1
        if dropped == 1 {
            let reason = CMGetAttachment(
                sampleBuffer,
                key: kCMSampleBufferAttachmentKey_DroppedFrameReason,
                attachmentModeOut: nil
            )
            emit(["event": "first_drop", "reason": reason.map { String(describing: $0) } ?? "unknown"])
        }
    }

    /// Give `body` the locked buffer's base address, or warn if its layout is unusable.
    func withFrame(
        _ sampleBuffer: CMSampleBuffer,
        _ body: (UnsafeRawPointer, Int, Int, Int, OSType) -> Void
    ) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            emit(["event": "warning", "message": "sample buffer has no image buffer", "index": seen])
            return
        }
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        let type = CVPixelBufferGetPixelFormatType(pixelBuffer)
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)

        guard !CVPixelBufferIsPlanar(pixelBuffer),
              bytesPerRow >= width * 2,
              let base = CVPixelBufferGetBaseAddress(pixelBuffer) else {
            emit([
                "event": "warning",
                "message": "unsupported buffer layout",
                "pixel_format": fourCC(type),
                "bytes_per_row": bytesPerRow,
                "width": width,
                "height": height,
            ])
            return
        }
        body(UnsafeRawPointer(base), width, height, bytesPerRow, type)
    }
}

/// Saves occasional frames to files, for diagnostics.
final class FileSink: CaptureSink {
    let outputDirectory: URL
    let saveEvery: Int
    var saved = 0

    init(outputDirectory: URL, saveEvery: Int, maxFrames: Int) {
        self.outputDirectory = outputDirectory
        self.saveEvery = saveEvery
        super.init(maxFrames: maxFrames)
    }

    override func handle(_ sampleBuffer: CMSampleBuffer, time: Double) {
        guard seen == 1 || seen % saveEvery == 0 else { return }
        withFrame(sampleBuffer) { base, width, height, bytesPerRow, type in
            let rowBytes = width * 2

            // Rows can carry padding, so copy them one at a time into a tight frame.
            var data = Data(count: rowBytes * height)
            data.withUnsafeMutableBytes { (target: UnsafeMutableRawBufferPointer) in
                guard let destination = target.baseAddress else { return }
                for row in 0..<height {
                    memcpy(destination + row * rowBytes, base + row * bytesPerRow, rowBytes)
                }
            }

            let file = outputDirectory.appendingPathComponent(String(format: "frame_%06d.raw", seen))
            do {
                // The atomic write means a reader never sees a half-written frame.
                try data.write(to: file, options: .atomic)
            } catch {
                emit(["event": "warning", "message": "could not save frame: \(error)", "index": seen])
                return
            }
            saved += 1
            emit([
                "event": "frame",
                "index": seen,
                "time": time,
                "path": file.path,
                "width": width,
                "height": height,
                "bytes_per_row": bytesPerRow,
                "pixel_format": fourCC(type),
            ])
        }
    }
}

/// A single-writer ring in a memory-mapped file.
///
/// Header (little-endian, 64 bytes):
///   0 magic "LEAP", 4 version, 8 width, 12 height, 16 slot_bytes, 20 slot_count,
///   24 latest_seq (u64), 32 slot_stride, 36 pixel format
/// Each slot:
///   0 seq_begin (u64), 8 seq_end (u64), 16 pts_ns (u64), 24 length (u32),
///   32 payload
///
/// The writer sets seq_begin, then the payload, then seq_end. A reader checks
/// seq_end before copying and seq_begin after, so an overwrite in progress can
/// never pass as a whole frame. leapmac/ring.py is the reader.
final class FrameRing {
    static let headerSize = 64
    static let slotHeaderSize = 32
    static let version: UInt32 = 1

    let base: UnsafeMutableRawPointer
    let width: Int
    let height: Int
    let slotBytes: Int
    let slotCount: Int
    let slotStride: Int
    private(set) var sequence: UInt64 = 0

    init?(path: String, width: Int, height: Int, slotCount: Int, pixelFormat: OSType) {
        let payloadBytes = width * height * 2
        // Keep payloads aligned to a cache line.
        let stride = (FrameRing.slotHeaderSize + payloadBytes + 63) & ~63
        let totalSize = FrameRing.headerSize + slotCount * stride

        self.width = width
        self.height = height
        self.slotCount = slotCount
        slotBytes = payloadBytes
        slotStride = stride

        let descriptor = open(path, O_RDWR | O_CREAT | O_TRUNC, 0o600)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        guard ftruncate(descriptor, off_t(totalSize)) == 0 else { return nil }
        guard let mapped = mmap(nil, totalSize, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0),
              mapped != MAP_FAILED else { return nil }
        base = mapped

        base.storeBytes(of: FrameRing.version, toByteOffset: 4, as: UInt32.self)
        base.storeBytes(of: UInt32(width), toByteOffset: 8, as: UInt32.self)
        base.storeBytes(of: UInt32(height), toByteOffset: 12, as: UInt32.self)
        base.storeBytes(of: UInt32(payloadBytes), toByteOffset: 16, as: UInt32.self)
        base.storeBytes(of: UInt32(slotCount), toByteOffset: 20, as: UInt32.self)
        base.storeBytes(of: UInt32(stride), toByteOffset: 32, as: UInt32.self)
        base.storeBytes(of: pixelFormat, toByteOffset: 36, as: UInt32.self)
        // The magic goes last, so a reader never sees a half-written header.
        OSMemoryBarrier()
        base.storeBytes(of: UInt32(0x5041_454C), toByteOffset: 0, as: UInt32.self)
    }

    func write(_ source: UnsafeRawPointer, bytesPerRow: Int, timeNanoseconds: UInt64) {
        sequence += 1
        let offset = FrameRing.headerSize + Int(sequence % UInt64(slotCount)) * slotStride
        let slot = base + offset
        let payload = slot + FrameRing.slotHeaderSize
        let rowBytes = width * 2

        slot.storeBytes(of: sequence, toByteOffset: 0, as: UInt64.self)
        OSMemoryBarrier()

        for row in 0..<height {
            memcpy(payload + row * rowBytes, source + row * bytesPerRow, rowBytes)
        }
        slot.storeBytes(of: timeNanoseconds, toByteOffset: 16, as: UInt64.self)
        slot.storeBytes(of: UInt32(slotBytes), toByteOffset: 24, as: UInt32.self)

        OSMemoryBarrier()
        slot.storeBytes(of: sequence, toByteOffset: 8, as: UInt64.self)
        OSMemoryBarrier()
        base.storeBytes(of: sequence, toByteOffset: 24, as: UInt64.self)
    }
}

/// Publishes every frame to the ring.
final class RingSink: CaptureSink {
    let ring: FrameRing
    var warnedAboutSize = false

    init(ring: FrameRing, maxFrames: Int) {
        self.ring = ring
        super.init(maxFrames: maxFrames)
    }

    override func handle(_ sampleBuffer: CMSampleBuffer, time: Double) {
        let timeNanoseconds = time.isFinite && time > 0 ? UInt64(time * 1_000_000_000) : 0
        withFrame(sampleBuffer) { base, width, height, bytesPerRow, _ in
            guard width == ring.width, height == ring.height else {
                if !warnedAboutSize {
                    warnedAboutSize = true
                    emit([
                        "event": "warning",
                        "message": "frame size differs from the ring",
                        "width": width,
                        "height": height,
                    ])
                }
                return
            }
            ring.write(base, bytesPerRow: bytesPerRow, timeNanoseconds: timeNanoseconds)
        }
    }
}

// MARK: - Capture session

struct CaptureSettings {
    let device: AVCaptureDevice
    let format: AVCaptureDevice.Format
    let pixelFormat: OSType
    let formatName: String
    let width: Int
    let height: Int
    let authorization: AVAuthorizationStatus
}

/// Check permission and find the device and format, failing with a clear event.
func resolveSettings(_ arguments: Arguments) -> CaptureSettings {
    guard let uniqueID = arguments.string("--unique-id") else { fail("--unique-id is required", code: 2) }

    let width = arguments.int("--width", default: 640)
    let height = arguments.int("--height", default: 480)
    let formatName = arguments.string("--pixel-format") ?? "yuvs"
    guard let requested = pixelFormat(named: formatName) else {
        fail("--pixel-format must be yuvs or 2vuy", code: 2)
    }

    let status = ensureCameraAccess()
    guard status == .authorized else {
        fail(
            "camera access is \(authorizationName(status)); grant it to the launching app in System Settings",
            code: 5,
            extra: ["authorization": authorizationName(status)]
        )
    }

    guard let device = discoverDevices().first(where: { $0.uniqueID == uniqueID }) else {
        fail("no capture device with unique id \(uniqueID)", code: 3)
    }
    guard let format = pickFormat(device, width: Int32(width), height: Int32(height), prefer: requested) else {
        fail("device has no \(width)x\(height) format", code: 4, extra: ["device": describe(device)])
    }
    return CaptureSettings(
        device: device,
        format: format,
        pixelFormat: requested,
        formatName: formatName,
        width: width,
        height: height,
        authorization: status
    )
}

func startSession(
    _ settings: CaptureSettings,
    arguments: Arguments,
    sink: CaptureSink,
    queue: DispatchQueue
) -> AVCaptureSession {
    let device = settings.device
    let session = AVCaptureSession()
    let output = AVCaptureVideoDataOutput()

    session.beginConfiguration()
    do {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { fail("session rejected the device input", code: 4) }
        session.addInput(input)
    } catch {
        fail("could not open device: \(error)", code: 4)
    }

    guard session.canAddOutput(output) else { fail("session rejected the video output", code: 4) }
    session.addOutput(output)

    let available = output.availableVideoPixelFormatTypes
    let availableNames = available.map { fourCC($0) }
    guard available.contains(settings.pixelFormat) else {
        fail(
            "output does not offer \(settings.formatName)",
            code: 4,
            extra: ["available_pixel_formats": availableNames]
        )
    }
    output.alwaysDiscardsLateVideoFrames = true
    output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: NSNumber(value: settings.pixelFormat)]
    output.setSampleBufferDelegate(sink, queue: queue)

    // Keep the lock until the session is running, so it cannot swap the format.
    do {
        try device.lockForConfiguration()
    } catch {
        fail("could not lock device: \(error)", code: 4)
    }
    device.activeFormat = settings.format
    if let fps = arguments.double("--fps") {
        let nearest = settings.format.videoSupportedFrameRateRanges.min {
            abs($0.maxFrameRate - fps) < abs($1.maxFrameRate - fps)
        }
        if let range = nearest {
            device.activeVideoMinFrameDuration = range.minFrameDuration
            device.activeVideoMaxFrameDuration = range.minFrameDuration
        }
    }
    session.commitConfiguration()
    logNotifications(from: session, source: "session")
    logNotifications(from: device, source: "device")
    session.startRunning()
    device.unlockForConfiguration()

    emit([
        "event": "started",
        "requested_pixel_format": settings.formatName,
        "available_pixel_formats": availableNames,
        "active_format": describe(device.activeFormat),
        "authorization": authorizationName(settings.authorization),
        "session_running": session.isRunning,
    ])
    return session
}

/// Wait for the sink to finish, the deadline to pass, or a stop request.
func runUntilFinished(_ sink: CaptureSink, seconds: Double) {
    // Keep the main run loop turning: parts of macOS capture may depend on it.
    let deadline = seconds.isFinite ? Date(timeIntervalSinceNow: seconds) : Date.distantFuture
    while Date() < deadline {
        if sink.finished.wait(timeout: .now() + .milliseconds(1)) == .success {
            break
        }
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
}

func stopAndReport(
    _ session: AVCaptureSession,
    sink: CaptureSink,
    queue: DispatchQueue,
    extra: @escaping () -> [String: Any] = { [:] }
) -> Never {
    session.stopRunning()

    // Waiting on the queue guarantees the delegate is idle before reading its counters.
    let summary: [String: Any] = queue.sync {
        var values = extra()
        values["event"] = "done"
        values["frames_seen"] = sink.seen
        values["frames_dropped"] = sink.dropped
        if let first = sink.firstTime, let last = sink.lastTime, sink.seen > 1, last > first {
            values["measured_fps"] = Double(sink.seen - 1) / (last - first)
        }
        return values
    }
    emit(summary)
    exit(sink.seen > 0 ? 0 : 6)
}

// MARK: - Commands

func runCapture(_ arguments: Arguments) -> Never {
    guard let outputPath = arguments.string("--out") else { fail("--out is required", code: 2) }
    let settings = resolveSettings(arguments)

    let outputDirectory = URL(fileURLWithPath: outputPath, isDirectory: true)
    do {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
    } catch {
        fail("could not create \(outputPath): \(error)", code: 2)
    }

    let queue = DispatchQueue(label: "leapcap.capture")
    let sink = FileSink(
        outputDirectory: outputDirectory,
        saveEvery: max(1, arguments.int("--every", default: 10)),
        maxFrames: arguments.int("--max-frames", default: 0)
    )
    let session = startSession(settings, arguments: arguments, sink: sink, queue: queue)
    runUntilFinished(sink, seconds: arguments.double("--seconds") ?? 15)
    stopAndReport(session, sink: sink, queue: queue, extra: { ["frames_saved": sink.saved] })
}

func runStream(_ arguments: Arguments) -> Never {
    guard let ringPath = arguments.string("--ring") else { fail("--ring is required", code: 2) }
    let settings = resolveSettings(arguments)

    guard let ring = FrameRing(
        path: ringPath,
        width: settings.width,
        height: settings.height,
        slotCount: max(2, arguments.int("--slots", default: 4)),
        pixelFormat: settings.pixelFormat
    ) else {
        fail("could not create the ring at \(ringPath)", code: 2)
    }

    let queue = DispatchQueue(label: "leapcap.capture")
    let sink = RingSink(ring: ring, maxFrames: arguments.int("--max-frames", default: 0))

    // Closing stdin stops streaming, so a crashed parent can't leave the camera held.
    // Ctrl-C in Terminal reaches the whole process group. Ignore it here and let
    // the parent stop us through stdin, so the "done" counts still get reported.
    signal(SIGINT, SIG_IGN)
    let finished = sink.finished
    DispatchQueue.global().async {
        while let line = readLine() {
            if line.trimmingCharacters(in: .whitespaces) == "stop" { break }
        }
        finished.signal()
    }

    let session = startSession(settings, arguments: arguments, sink: sink, queue: queue)
    emit(["event": "ready", "ring": ringPath])
    runUntilFinished(sink, seconds: arguments.double("--seconds") ?? .infinity)
    stopAndReport(session, sink: sink, queue: queue, extra: { ["frames_published": ring.sequence] })
}

func runList() -> Never {
    let status = ensureCameraAccess()
    emit([
        "event": "devices",
        "authorization": authorizationName(status),
        "devices": discoverDevices().map { describe($0) },
    ])
    exit(0)
}

// MARK: - Entry point

let commandLine = Array(CommandLine.arguments.dropFirst())
switch commandLine.first {
case "list":
    runList()
case "capture":
    runCapture(Arguments(values: Array(commandLine.dropFirst())))
case "stream":
    runStream(Arguments(values: Array(commandLine.dropFirst())))
default:
    fail("usage: leapcap list | capture --unique-id ID --out DIR | stream --unique-id ID --ring PATH", code: 2)
}
