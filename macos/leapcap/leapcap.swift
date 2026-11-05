// leapcap: AVFoundation capture helper for an unlocked Leap Motion Controller.
//
// OpenCV's macOS camera backend always asks AVFoundation for BGRA, which
// destroys the Leap's interleaved left/right bytes. This helper asks for the
// 2-byte-per-pixel YUV layouts instead and copies the bytes untouched.
//
// Phase 0 commands:
//   leapcap list
//   leapcap capture --unique-id ID --out DIR [--width 640] [--height 480]
//                   [--pixel-format yuvs|2vuy] [--fps N] [--seconds 15]
//                   [--every 10] [--max-frames N]
//
// Every line on stdout is one JSON object with an "event" key.
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

// MARK: - Capture

final class FrameSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let outputDirectory: URL
    let saveEvery: Int
    let maxFrames: Int
    let finished = DispatchSemaphore(value: 0)

    // These are touched only on the capture queue until the session stops.
    var seen = 0
    var saved = 0
    var dropped = 0
    var firstTime: Double?
    var lastTime: Double?
    var signalled = false

    init(outputDirectory: URL, saveEvery: Int, maxFrames: Int) {
        self.outputDirectory = outputDirectory
        self.saveEvery = saveEvery
        self.maxFrames = maxFrames
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        seen += 1
        let time = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        if firstTime == nil { firstTime = time }
        lastTime = time

        if seen == 1 || seen % saveEvery == 0 {
            save(sampleBuffer, time: time)
        }
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
    }

    private func save(_ sampleBuffer: CMSampleBuffer, time: Double) {
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
        let rowBytes = width * 2

        guard !CVPixelBufferIsPlanar(pixelBuffer),
              bytesPerRow >= rowBytes,
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

func runCapture(_ arguments: Arguments) -> Never {
    guard let uniqueID = arguments.string("--unique-id") else { fail("--unique-id is required", code: 2) }
    guard let outputPath = arguments.string("--out") else { fail("--out is required", code: 2) }

    let width = arguments.int("--width", default: 640)
    let height = arguments.int("--height", default: 480)
    let seconds = arguments.double("--seconds") ?? 15
    let saveEvery = max(1, arguments.int("--every", default: 10))
    let maxFrames = arguments.int("--max-frames", default: 0)
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

    let outputDirectory = URL(fileURLWithPath: outputPath, isDirectory: true)
    do {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
    } catch {
        fail("could not create \(outputPath): \(error)", code: 2)
    }

    let session = AVCaptureSession()
    let output = AVCaptureVideoDataOutput()
    let queue = DispatchQueue(label: "leapcap.capture")
    let sink = FrameSink(outputDirectory: outputDirectory, saveEvery: saveEvery, maxFrames: maxFrames)

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
    guard available.contains(requested) else {
        fail(
            "output does not offer \(formatName)",
            code: 4,
            extra: ["available_pixel_formats": availableNames]
        )
    }
    output.alwaysDiscardsLateVideoFrames = true
    output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: NSNumber(value: requested)]
    output.setSampleBufferDelegate(sink, queue: queue)

    // Keep the lock until the session is running, so it cannot swap the format.
    do {
        try device.lockForConfiguration()
    } catch {
        fail("could not lock device: \(error)", code: 4)
    }
    device.activeFormat = format
    if let fps = arguments.double("--fps") {
        let nearest = format.videoSupportedFrameRateRanges.min {
            abs($0.maxFrameRate - fps) < abs($1.maxFrameRate - fps)
        }
        if let range = nearest {
            device.activeVideoMinFrameDuration = range.minFrameDuration
            device.activeVideoMaxFrameDuration = range.minFrameDuration
        }
    }
    session.commitConfiguration()
    session.startRunning()
    device.unlockForConfiguration()

    emit([
        "event": "started",
        "requested_pixel_format": formatName,
        "available_pixel_formats": availableNames,
        "active_format": describe(device.activeFormat),
        "authorization": authorizationName(status),
        "session_running": session.isRunning,
    ])

    _ = sink.finished.wait(timeout: .now() + .milliseconds(Int(seconds * 1000)))
    session.stopRunning()

    // Waiting on the queue guarantees the delegate is idle before reading its counters.
    let summary: [String: Any] = queue.sync {
        var values: [String: Any] = [
            "event": "done",
            "frames_seen": sink.seen,
            "frames_saved": sink.saved,
            "frames_dropped": sink.dropped,
        ]
        if let first = sink.firstTime, let last = sink.lastTime, sink.seen > 1, last > first {
            values["measured_fps"] = Double(sink.seen - 1) / (last - first)
        }
        return values
    }
    emit(summary)
    exit(sink.seen > 0 ? 0 : 6)
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
default:
    fail("usage: leapcap list | leapcap capture --unique-id ID --out DIR [options]", code: 2)
}
