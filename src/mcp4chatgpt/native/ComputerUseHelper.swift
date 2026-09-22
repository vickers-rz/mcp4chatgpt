import AppKit
import ApplicationServices
import Foundation
import ScreenCaptureKit

// One serial process owns all AX references. A new observation invalidates the old snapshot.
struct HelperFailure: Error {
    let code: String
    let effect: String
    init(_ code: String, effect: String = "not_started") { self.code = code; self.effect = effect }
}

struct Snapshot {
    let id: String
    let appID: String
    let pid: pid_t
    let launched: Date
    let windowID: String
    let window: AXUIElement
    let created: Date
    var elements: [String: AXUIElement]
}

var snapshots: [String: Snapshot] = [:]
struct SelectionRequired: Error { let result: [String: Any] }
struct WindowRecord {
    let appID: String
    let pid: pid_t
    let launched: Date
    let window: AXUIElement
}
var windowRecords: [String: WindowRecord] = [:]

struct WindowServerRecord {
    let id: CGWindowID
    let bounds: CGRect
    let title: String
    let isOnScreen: Bool
}

struct DisplayRecord {
    let cgID: CGDirectDisplayID
    let sourceWidth: Int
    let sourceHeight: Int
    let bounds: CGRect
}

struct DisplaySnapshot {
    let id: String
    let displayID: String
    let cgID: CGDirectDisplayID
    let sourceWidth: Int
    let sourceHeight: Int
    let bounds: CGRect
    let created: Date
}

var displayRecords: [String: DisplayRecord] = [:]
var displaySnapshots: [String: DisplaySnapshot] = [:]

func value(_ element: AXUIElement, _ attribute: CFString) -> CFTypeRef? {
    var result: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, attribute, &result) == .success ? result : nil
}

func string(_ element: AXUIElement, _ attribute: CFString) -> String? {
    value(element, attribute) as? String
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    value(element, kAXChildrenAttribute as CFString) as? [AXUIElement] ?? []
}

func axPoint(_ element: AXUIElement, _ attribute: CFString) -> CGPoint? {
    guard let raw = value(element, attribute) else { return nil }
    let axValue = raw as! AXValue
    guard AXValueGetType(axValue) == .cgPoint else { return nil }
    var point = CGPoint.zero
    return AXValueGetValue(axValue, .cgPoint, &point) ? point : nil
}

func axSize(_ element: AXUIElement, _ attribute: CFString) -> CGSize? {
    guard let raw = value(element, attribute) else { return nil }
    let axValue = raw as! AXValue
    guard AXValueGetType(axValue) == .cgSize else { return nil }
    var size = CGSize.zero
    return AXValueGetValue(axValue, .cgSize, &size) ? size : nil
}

func axFrame(_ element: AXUIElement) -> CGRect? {
    guard let origin = axPoint(element, kAXPositionAttribute as CFString),
          let size = axSize(element, kAXSizeAttribute as CFString),
          size.width > 0, size.height > 0 else { return nil }
    return CGRect(origin: origin, size: size)
}

func framesNear(_ first: CGRect, _ second: CGRect, tolerance: CGFloat = 4) -> Bool {
    abs(first.minX - second.minX) <= tolerance &&
    abs(first.minY - second.minY) <= tolerance &&
    abs(first.width - second.width) <= tolerance &&
    abs(first.height - second.height) <= tolerance
}

func parent(_ element: AXUIElement) -> AXUIElement? {
    value(element, kAXParentAttribute as CFString) as! AXUIElement?
}

func enclosingWindow(_ element: AXUIElement, pid: pid_t, maxDepth: Int = 24) -> AXUIElement? {
    var current: AXUIElement? = element
    var depth = 0
    while let item = current, depth <= maxDepth {
        var elementPID: pid_t = 0
        guard AXUIElementGetPid(item, &elementPID) == .success, elementPID == pid else { return nil }
        if string(item, kAXRoleAttribute as CFString) == (kAXWindowRole as String) { return item }
        current = parent(item)
        depth += 1
    }
    return nil
}

func windowServerRecords(pid: pid_t) -> [WindowServerRecord] {
    let rows = CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements],
        kCGNullWindowID
    ) as? [[String: Any]] ?? []
    var records: [WindowServerRecord] = []
    for row in rows {
        guard
            (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
            (row[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
            let number = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
            let rawBounds = row[kCGWindowBounds as String]
        else { continue }
        let dictionary = rawBounds as! CFDictionary
        guard let rect = CGRect(dictionaryRepresentation: dictionary),
              rect.width >= 100, rect.height >= 80 else { continue }
        records.append(
            WindowServerRecord(
                id: number,
                bounds: rect,
                title: row[kCGWindowName as String] as? String ?? "",
                isOnScreen: (row[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false
            )
        )
        if records.count >= 64 { break }
    }
    return records
}

func windowServerRecord(
    for window: AXUIElement,
    records: [WindowServerRecord]
) -> WindowServerRecord? {
    if let explicit = value(window, "AXWindowNumber" as CFString) as? NSNumber,
       let exact = records.first(where: { $0.id == explicit.uint32Value }) {
        return exact
    }
    guard let frame = axFrame(window), frame.width >= 100, frame.height >= 80 else {
        return nil
    }
    let title = string(window, kAXTitleAttribute as CFString) ?? ""
    return records
        .filter { framesNear(frame, $0.bounds, tolerance: 6) }
        .max { left, right in
            let leftTitle = !title.isEmpty && left.title == title ? 1 : 0
            let rightTitle = !title.isEmpty && right.title == title ? 1 : 0
            if leftTitle != rightTitle { return leftTitle < rightTitle }
            return left.isOnScreen == false && right.isOnScreen == true
        }
}

func visibleWindowBounds(pid: pid_t) -> [CGRect] {
    windowServerRecords(pid: pid)
        .filter { $0.isOnScreen }
        .map { $0.bounds }
}

func hitTestWindow(_ app: AXUIElement, pid: pid_t, rect: CGRect) -> AXUIElement? {
    let points = [
        CGPoint(x: rect.midX, y: rect.midY),
        CGPoint(x: rect.minX + rect.width * 0.25, y: rect.minY + rect.height * 0.25),
        CGPoint(x: rect.minX + rect.width * 0.75, y: rect.minY + rect.height * 0.25),
        CGPoint(x: rect.minX + rect.width * 0.25, y: rect.minY + rect.height * 0.75),
        CGPoint(x: rect.minX + rect.width * 0.75, y: rect.minY + rect.height * 0.75),
    ]
    for point in points {
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(app, Float(point.x), Float(point.y), &hit) == .success,
              let hit, let window = enclosingWindow(hit, pid: pid) else { continue }
        return window
    }
    return nil
}

func allWindows(_ app: AXUIElement, pid: pid_t) -> [AXUIElement] {
    // AXWindows can contain tiny utility surfaces that also appear as layer-0
    // WindowServer entries. Pair normal AX windows with a substantial CGWindow
    // root instead of trusting every AXWindow blindly.
    let serverRecords = windowServerRecords(pid: pid)
    let candidates = value(app, kAXWindowsAttribute as CFString) as? [AXUIElement] ?? []
    let direct = candidates.filter {
        !axSame($0, app) &&
        string($0, kAXRoleAttribute as CFString) == (kAXWindowRole as String)
    }
    let paired = direct.filter { window in
        if (value(window, kAXMinimizedAttribute as CFString) as? NSNumber)?.boolValue == true {
            guard let frame = axFrame(window) else { return false }
            return frame.width >= 100 && frame.height >= 80
        }
        return windowServerRecord(for: window, records: serverRecords) != nil
    }
    if !paired.isEmpty { return paired }

    var recovered: [AXUIElement] = []
    for record in serverRecords where record.isOnScreen {
        guard let window = hitTestWindow(app, pid: pid, rect: record.bounds),
              !recovered.contains(where: { axSame($0, window) }) else { continue }
        recovered.append(window)
        if recovered.count >= 32 { break }
    }
    return recovered
}

func axSame(_ first: AXUIElement, _ second: AXUIElement) -> Bool { CFEqual(first, second) }

func contains(_ root: AXUIElement, _ target: AXUIElement, max: Int = 2500) -> Bool {
    var queue = [root]
    var index = 0
    while index < queue.count && index < max {
        let current = queue[index]
        if axSame(current, target) { return true }
        index += 1
        if queue.count < max { queue.append(contentsOf: children(current).prefix(max - queue.count)) }
    }
    return false
}

func allowed(_ args: [String: Any]) throws -> [String] {
    guard let apps = args["allowed_apps"] as? [String], !apps.isEmpty, apps.count <= 64,
          apps.allSatisfy({ $0 == "*" || ($0.count < 200 && $0.contains(".")) }) else { throw HelperFailure("invalid_allowlist") }
    return apps
}

func targetApp(_ args: [String: Any]) throws -> (String, NSRunningApplication, AXUIElement) {
    let apps = try allowed(args)
    guard let id = args["app_id"] as? String, apps.contains("*") || apps.contains(id) else { throw HelperFailure("app_not_allowed") }
    let matches = NSRunningApplication.runningApplications(withBundleIdentifier: id).filter { !$0.isTerminated }
    guard !matches.isEmpty else { throw HelperFailure("app_not_running") }
    let windowRecord = (args["window_id"] as? String).flatMap { windowRecords[$0] }
    let pid = args["pid"] as? Int ?? windowRecord.map { Int($0.pid) }
    let running: NSRunningApplication
    if let pid {
        guard let exact = matches.first(where: { Int($0.processIdentifier) == pid }) else { throw HelperFailure("stale_process") }
        running = exact
    } else {
        guard matches.count == 1 else {
            throw SelectionRequired(result: ["status": "process_selection_required", "app_id": id, "processes": matches.prefix(64).map { ["pid": Int($0.processIdentifier), "name": $0.localizedName ?? ""] }, "truncated": matches.count > 64])
        }
        running = matches[0]
    }
    guard running.launchDate != nil else { throw HelperFailure("process_identity_unavailable") }
    guard AXIsProcessTrusted() else { throw HelperFailure("accessibility_permission_required") }
    return (id, running, AXUIElementCreateApplication(running.processIdentifier))
}

func selectedWindow(_ app: AXUIElement, running: NSRunningApplication, requested: String?) throws -> (String, AXUIElement, [[String: Any]]) {
    let pid = running.processIdentifier
    let appID = running.bundleIdentifier!
    let launched = running.launchDate!
    // Materialize once: candidate metadata and the selected AX reference agree.
    let windows = allWindows(app, pid: pid)
    windowRecords = windowRecords.filter { _, record in
        record.pid != pid || (record.launched == launched && windows.contains { axSame($0, record.window) })
    }
    let entries: [(String, AXUIElement)] = windows.prefix(32).map { window in
        if let existing = windowRecords.first(where: { $0.value.pid == pid && $0.value.launched == launched && axSame($0.value.window, window) }) { return (existing.key, window) }
        let token = UUID().uuidString
        windowRecords[token] = WindowRecord(appID: appID, pid: pid, launched: launched, window: window)
        return (token, window)
    }
    let available: [[String: Any]] = entries.map { token, window in
        ["window_id": token, "title": String((string(window, kAXTitleAttribute as CFString) ?? "").prefix(300)), "minimized": (value(window, kAXMinimizedAttribute as CFString) as? NSNumber)?.boolValue ?? false]
    }
    guard let requested, !requested.isEmpty else {
        if entries.count == 1 { return (entries[0].0, entries[0].1, available) }
        if entries.isEmpty { throw HelperFailure("no_window") }
        throw SelectionRequired(result: ["status": "window_selection_required", "app_id": appID, "pid": Int(pid), "windows": available, "truncated": windows.count > 32])
    }
    for (token, window) in entries where token == requested {
        return (token, window, available)
    }
    throw HelperFailure("stale_window")
}

func requireSnapshot(_ args: [String: Any], elementID: String? = nil) throws -> (Snapshot, AXUIElement?) {
    guard let token = args["snapshot_id"] as? String,
          let snapshot = snapshots.values.first(where: { $0.id == token }),
          snapshot.appID == args["app_id"] as? String,
          (args["pid"] as? Int).map({ $0 == Int(snapshot.pid) }) ?? true,
          snapshot.windowID == args["window_id"] as? String,
          Date().timeIntervalSince(snapshot.created) < 45 else { throw HelperFailure("stale_snapshot") }
    var bound = args
    bound["pid"] = Int(snapshot.pid)
    let (_, running, app) = try targetApp(bound)
    guard snapshot.launched == running.launchDate else { throw HelperFailure("stale_process") }
    guard allWindows(app, pid: running.processIdentifier).contains(where: { axSame($0, snapshot.window) }) else { throw HelperFailure("stale_window") }
    if let elementID {
        guard let element = snapshot.elements[elementID], contains(snapshot.window, element) else { throw HelperFailure("stale_element") }
        return (snapshot, element)
    }
    return (snapshot, nil)
}

func state(_ args: [String: Any]) throws -> [String: Any] {
    let (id, running, app) = try targetApp(args)
    let (windowID, window, available) = try selectedWindow(app, running: running, requested: args["window_id"] as? String)
    let limit = max(1, min(args["max_elements"] as? Int ?? 200, 400))
    let snapshotID = UUID().uuidString
    var objects: [String: AXUIElement] = [:]
    var output: [[String: Any]] = []
    var queue: [(AXUIElement, Int)] = [(window, 0)]
    var next = 0
    var omitted = false
    while next < queue.count && output.count < limit {
        let (element, depth) = queue[next]
        next += 1
        let eid = UUID().uuidString
        objects[eid] = element
        var item: [String: Any] = ["element_id": eid, "depth": depth]
        for (key, attribute) in [("role", kAXRoleAttribute), ("title", kAXTitleAttribute), ("description", kAXDescriptionAttribute), ("value", kAXValueAttribute)] {
            if let text = string(element, attribute as CFString), !text.isEmpty { item[key] = String(text.prefix(key == "value" ? 1000 : 300)) }
        }
        if let enabled = value(element, kAXEnabledAttribute as CFString) as? NSNumber { item["enabled"] = enabled.boolValue }
        if let frame = axFrame(element) {
            item["frame"] = ["x": frame.origin.x, "y": frame.origin.y, "width": frame.width, "height": frame.height]
        }
        output.append(item)
        let childElements = children(element)
        let room = max(0, 1600 - queue.count)
        if depth >= 12 { omitted = omitted || !childElements.isEmpty }
        else {
            omitted = omitted || childElements.count > room
            queue.append(contentsOf: childElements.prefix(room).map { ($0, depth + 1) })
        }
    }
    snapshots["\(id):\(running.processIdentifier)"] = Snapshot(id: snapshotID, appID: id, pid: running.processIdentifier, launched: running.launchDate!, windowID: windowID, window: window, created: Date(), elements: objects)
    return ["status": "ok", "app_id": id, "pid": Int(running.processIdentifier), "window_id": windowID, "windows": available, "snapshot_id": snapshotID, "elements": output, "truncated": omitted || next < queue.count]
}

func screenshot(_ args: [String: Any]) throws -> [String: Any] {
    let (id, running, app) = try targetApp(args)
    guard args["window_id"] is String else { throw HelperFailure("window_id_required") }
    let (windowID, axWindow, _) = try selectedWindow(app, running: running, requested: args["window_id"] as? String)
    guard (value(axWindow, kAXMinimizedAttribute as CFString) as? NSNumber)?.boolValue != true else { throw HelperFailure("window_not_visible") }
    let title = string(axWindow, kAXTitleAttribute as CFString) ?? ""
    let number = value(axWindow, "AXWindowNumber" as CFString) as? NSNumber
    let frame = axFrame(axWindow)
    if number == nil && frame == nil && title.isEmpty {
        throw HelperFailure("window_ambiguous")
    }
    guard CGPreflightScreenCaptureAccess() else { throw HelperFailure("screen_recording_permission_required") }
    let semaphore = DispatchSemaphore(value: 0)
    var captured: CGImage?
    Task {
        defer { semaphore.signal() }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            let owned = content.windows.filter { $0.owningApplication?.processID == running.processIdentifier }
            let scWindow: SCWindow?
            if let number {
                scWindow = owned.first { $0.windowID == number.uint32Value }
            } else if let frame {
                let geometryMatches = owned.filter { framesNear($0.frame, frame) }
                if geometryMatches.count == 1 {
                    scWindow = geometryMatches[0]
                } else if geometryMatches.isEmpty {
                    let titleMatches = owned.filter { !title.isEmpty && $0.title == title }
                    scWindow = titleMatches.count == 1 ? titleMatches[0] : nil
                } else {
                    scWindow = nil
                }
            } else {
                let titleMatches = owned.filter { !title.isEmpty && $0.title == title }
                scWindow = titleMatches.count == 1 ? titleMatches[0] : nil
            }
            guard let scWindow, scWindow.isOnScreen else { return }
            let filter = SCContentFilter(desktopIndependentWindow: scWindow)
            let configuration = SCStreamConfiguration()
            let scale = min(1.0, 2048.0 / max(scWindow.frame.width, scWindow.frame.height, 1))
            configuration.width = Int(scWindow.frame.width * scale)
            configuration.height = Int(scWindow.frame.height * scale)
            guard configuration.width > 0 && configuration.height > 0 else { return }
            captured = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
        } catch { /* A bounded error is returned below; UI data never goes to stderr. */ }
    }
    guard semaphore.wait(timeout: .now() + 12) == .success, let image = captured else { throw HelperFailure("screenshot_unavailable") }
    let bitmap = NSBitmapImageRep(cgImage: image)
    guard let png = bitmap.representation(using: .png, properties: [:]), png.count <= 7_500_000 else { throw HelperFailure("screenshot_too_large") }
    return ["app_id": id, "window_id": windowID, "width": image.width, "height": image.height, "png_base64": png.base64EncodedString(), "captured_at": Date().timeIntervalSince1970]
}

func displayGeometry(_ cgID: CGDirectDisplayID) throws -> DisplayRecord {
    guard CGDisplayIsActive(cgID) != 0, let mode = CGDisplayCopyDisplayMode(cgID) else {
        throw HelperFailure("display_unavailable")
    }
    let width = mode.pixelWidth
    let height = mode.pixelHeight
    let bounds = CGDisplayBounds(cgID)
    guard width > 0, height > 0, bounds.width > 0, bounds.height > 0 else {
        throw HelperFailure("display_geometry_invalid")
    }
    return DisplayRecord(cgID: cgID, sourceWidth: width, sourceHeight: height, bounds: bounds)
}

func permissionStatus(_ args: [String: Any]) throws -> [String: Any] {
    _ = try allowed(args)
    return [
        "accessibility_trusted": AXIsProcessTrusted(),
        "screen_recording": CGPreflightScreenCaptureAccess(),
        "event_posting": CGPreflightPostEventAccess(),
    ]
}

func requestPermissions(_ args: [String: Any]) throws -> [String: Any] {
    _ = try allowed(args)
    let requestAccessibility = args["accessibility"] as? Bool ?? true
    let requestScreenRecording = args["screen_recording"] as? Bool ?? true
    let requestEventPosting = args["event_posting"] as? Bool ?? true

    if requestAccessibility {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }
    if requestScreenRecording && !CGPreflightScreenCaptureAccess() {
        _ = CGRequestScreenCaptureAccess()
    }
    if requestEventPosting && !CGPreflightPostEventAccess() {
        _ = CGRequestPostEventAccess()
    }

    return [
        "accessibility_trusted": AXIsProcessTrusted(),
        "screen_recording": CGPreflightScreenCaptureAccess(),
        "event_posting": CGPreflightPostEventAccess(),
        "requested_accessibility": requestAccessibility,
        "requested_screen_recording": requestScreenRecording,
        "requested_event_posting": requestEventPosting,
        "restart_may_be_required": true,
    ]
}

func listDisplays(_ args: [String: Any]) throws -> [String: Any] {
    guard try allowed(args).contains("*") else { throw HelperFailure("full_desktop_not_allowed") }
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else {
        throw HelperFailure("display_list_unavailable")
    }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    var actual = count
    let error = ids.withUnsafeMutableBufferPointer { buffer in
        CGGetActiveDisplayList(count, buffer.baseAddress, &actual)
    }
    guard error == .success else { throw HelperFailure("display_list_unavailable") }

    displayRecords.removeAll()
    displaySnapshots.removeAll()
    var output: [[String: Any]] = []
    let activeCount = min(Int(actual), ids.count, 16)
    for index in 0..<activeCount {
        let cgID = ids[index]
        let record = try displayGeometry(cgID)
        let token = UUID().uuidString
        displayRecords[token] = record
        output.append([
            "display_id": token,
            "source_width": record.sourceWidth,
            "source_height": record.sourceHeight,
            "logical_x": record.bounds.origin.x,
            "logical_y": record.bounds.origin.y,
            "logical_width": record.bounds.width,
            "logical_height": record.bounds.height,
            "primary": CGDisplayIsMain(cgID) != 0,
        ])
    }
    return ["displays": output, "count": output.count, "truncated": Int(actual) > output.count]
}

func screenshotDisplay(_ args: [String: Any]) throws -> [String: Any] {
    guard try allowed(args).contains("*") else { throw HelperFailure("full_desktop_not_allowed") }
    guard let displayID = args["display_id"] as? String,
          let record = displayRecords[displayID] else { throw HelperFailure("stale_display") }
    let current = try displayGeometry(record.cgID)
    guard current.sourceWidth == record.sourceWidth,
          current.sourceHeight == record.sourceHeight,
          current.bounds == record.bounds else { throw HelperFailure("stale_display") }
    guard CGPreflightScreenCaptureAccess() else { throw HelperFailure("screen_recording_permission_required") }

    let semaphore = DispatchSemaphore(value: 0)
    var captured: CGImage?
    Task {
        defer { semaphore.signal() }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first(where: { $0.displayID == record.cgID }) else { return }
            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let configuration = SCStreamConfiguration()
            let scale = min(1.0, 2560.0 / max(Double(current.sourceWidth), Double(current.sourceHeight), 1.0))
            configuration.width = max(1, Int(Double(current.sourceWidth) * scale))
            configuration.height = max(1, Int(Double(current.sourceHeight) * scale))
            configuration.showsCursor = true
            captured = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
        } catch { /* Return the bounded failure below. */ }
    }
    guard semaphore.wait(timeout: .now() + 12) == .success, let image = captured else {
        throw HelperFailure("screenshot_unavailable")
    }
    let bitmap = NSBitmapImageRep(cgImage: image)
    guard let png = bitmap.representation(using: .png, properties: [:]), png.count <= 7_500_000 else {
        throw HelperFailure("screenshot_too_large")
    }
    let snapshotID = UUID().uuidString
    displaySnapshots[displayID] = DisplaySnapshot(
        id: snapshotID,
        displayID: displayID,
        cgID: record.cgID,
        sourceWidth: current.sourceWidth,
        sourceHeight: current.sourceHeight,
        bounds: current.bounds,
        created: Date()
    )
    return [
        "display_id": displayID,
        "display_snapshot_id": snapshotID,
        "source_width": current.sourceWidth,
        "source_height": current.sourceHeight,
        "width": image.width,
        "height": image.height,
        "png_base64": png.base64EncodedString(),
        "captured_at": Date().timeIntervalSince1970,
    ]
}

func launchApp(_ args: [String: Any]) throws -> [String: Any] {
    let allowlist = try allowed(args)
    guard let id = args["app_id"] as? String,
          allowlist.contains("*") || allowlist.contains(id) else {
        throw HelperFailure("app_not_allowed")
    }
    if let existing = NSRunningApplication.runningApplications(withBundleIdentifier: id)
        .first(where: { !$0.isTerminated }) {
        return [
            "success": true,
            "effect": "completed",
            "app_id": id,
            "pid": Int(existing.processIdentifier),
            "already_running": true,
            "observe_again": true,
        ]
    }
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) else {
        throw HelperFailure("application_not_found")
    }

    let semaphore = DispatchSemaphore(value: 0)
    var launched: NSRunningApplication?
    var launchError: Error?
    let configuration = NSWorkspace.OpenConfiguration()
    NSWorkspace.shared.openApplication(at: url, configuration: configuration) { app, error in
        launched = app
        launchError = error
        semaphore.signal()
    }
    snapshots.removeAll()
    displaySnapshots.removeAll()
    guard semaphore.wait(timeout: .now() + 12) == .success else {
        throw HelperFailure("launch_timeout", effect: "outcome_unknown")
    }
    guard launchError == nil, let running = launched else {
        throw HelperFailure("launch_failed", effect: "outcome_unknown")
    }
    return [
        "success": true,
        "effect": "completed",
        "app_id": id,
        "pid": Int(running.processIdentifier),
        "already_running": false,
        "observe_again": true,
    ]
}

func listWindows(_ args: [String: Any]) throws -> [String: Any] {
    let (id, running, app) = try targetApp(args)
    let windows = allWindows(app, pid: running.processIdentifier)
    let serverRecords = windowServerRecords(pid: running.processIdentifier)
    let launched = running.launchDate!

    windowRecords = windowRecords.filter { _, record in
        record.pid != running.processIdentifier ||
        (record.launched == launched && windows.contains { axSame($0, record.window) })
    }

    let entries: [(String, AXUIElement)] = windows.prefix(32).map { window in
        if let existing = windowRecords.first(where: {
            $0.value.pid == running.processIdentifier &&
            $0.value.launched == launched &&
            axSame($0.value.window, window)
        }) {
            return (existing.key, window)
        }
        let token = "native-window:" + UUID().uuidString
        windowRecords[token] = WindowRecord(
            appID: id,
            pid: running.processIdentifier,
            launched: launched,
            window: window
        )
        return (token, window)
    }

    let focused = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?
    let main = value(app, kAXMainWindowAttribute as CFString) as! AXUIElement?
    let available: [[String: Any]] = entries.map { token, window in
        var item: [String: Any] = [
            "window_id": token,
            "title": String((string(window, kAXTitleAttribute as CFString) ?? "").prefix(300)),
            "minimized": (value(window, kAXMinimizedAttribute as CFString) as? NSNumber)?.boolValue ?? false,
            "focused": focused.map { axSame($0, window) } ?? false,
            "main": main.map { axSame($0, window) } ?? false,
        ]
        if let frame = axFrame(window) {
            item["bounds"] = [
                "x": frame.origin.x,
                "y": frame.origin.y,
                "width": frame.size.width,
                "height": frame.size.height,
            ]
        }
        if let server = windowServerRecord(for: window, records: serverRecords) {
            item["window_number"] = Int(server.id)
            item["on_screen"] = server.isOnScreen
        }
        return item
    }

    return [
        "status": "ok",
        "app_id": id,
        "pid": Int(running.processIdentifier),
        "windows": available,
        "truncated": windows.count > 32,
    ]
}

func selectWindowBackground(_ args: [String: Any]) throws -> [String: Any] {
    let (id, running, app) = try targetApp(args)
    guard args["window_id"] is String else { throw HelperFailure("window_id_required") }
    let (windowID, window, available) = try selectedWindow(
        app,
        running: running,
        requested: args["window_id"] as? String
    )

    let frontmostBefore = NSWorkspace.shared.frontmostApplication?.processIdentifier
    let previousFocused = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?
    let previousWindowID = previousFocused.flatMap { previous in
        available.first(where: { row in
            guard let token = row["window_id"] as? String,
                  let record = windowRecords[token] else { return false }
            return axSame(record.window, previous)
        })?["window_id"] as? String
    }

    let setFocused = AXUIElementSetAttributeValue(
        app,
        kAXFocusedWindowAttribute as CFString,
        window
    )
    if setFocused != .success && setFocused != .attributeUnsupported && setFocused != .notImplemented {
        throw HelperFailure("background_focus_failed_\(setFocused.rawValue)")
    }

    let setMain = AXUIElementSetAttributeValue(
        window,
        kAXMainAttribute as CFString,
        kCFBooleanTrue
    )
    if setMain != .success && setMain != .attributeUnsupported && setMain != .notImplemented {
        throw HelperFailure("background_main_failed_\(setMain.rawValue)")
    }

    var verified = false
    for _ in 0..<20 {
        let frontmostNow = NSWorkspace.shared.frontmostApplication?.processIdentifier
        if frontmostNow != frontmostBefore {
            if let previousWindowID,
               let previous = windowRecords[previousWindowID] {
                _ = AXUIElementSetAttributeValue(
                    app,
                    kAXFocusedWindowAttribute as CFString,
                    previous.window
                )
            }
            throw HelperFailure("background_focus_stole_frontmost", effect: "outcome_unknown")
        }
        if let focusedNow = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?,
           axSame(focusedNow, window) {
            verified = true
            break
        }
        usleep(10_000)
    }

    guard verified else {
        throw HelperFailure("background_window_selection_unverified")
    }
    snapshots.removeAll()
    return [
        "success": true,
        "effect": "completed",
        "app_id": id,
        "pid": Int(running.processIdentifier),
        "window_id": windowID,
        "previous_window_id": previousWindowID as Any,
        "frontmost_pid": frontmostBefore.map { Int($0) } as Any,
    ]
}

func activateWindow(_ args: [String: Any]) throws -> [String: Any] {
    let (id, running, app) = try targetApp(args)
    guard args["window_id"] is String else { throw HelperFailure("window_id_required") }
    let (windowID, window, _) = try selectedWindow(app, running: running, requested: args["window_id"] as? String)

    let accepted = running.activate(options: [])
    guard accepted else { throw HelperFailure("activate_failed", effect: "outcome_unknown") }
    let raised = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
    guard raised == .success else { throw HelperFailure("raise_failed_\(raised.rawValue)", effect: "outcome_unknown") }

    var verified = false
    for _ in 0..<20 {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == running.processIdentifier,
           let focused = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?,
           axSame(focused, window) {
            verified = true
            break
        }
        usleep(25_000)
    }
    snapshots.removeValue(forKey: "\(id):\(running.processIdentifier)")
    guard verified else { throw HelperFailure("activation_unverified", effect: "outcome_unknown") }
    return ["success": true, "effect": "completed", "app_id": id, "pid": Int(running.processIdentifier), "window_id": windowID, "observe_again": true]
}

func cursorReached(_ target: CGPoint) -> Bool {
    for _ in 0..<20 {
        if let probe = CGEvent(source: nil) {
            let point = probe.location
            if abs(point.x - target.x) <= 1.0 && abs(point.y - target.y) <= 1.0 { return true }
        }
        usleep(10_000)
    }
    return false
}

func displaySnapshotContext(_ args: [String: Any]) throws -> (String, DisplaySnapshot, DisplayRecord) {
    guard try allowed(args).contains("*") else { throw HelperFailure("full_desktop_not_allowed") }
    guard let displayID = args["display_id"] as? String,
          let snapshotID = args["display_snapshot_id"] as? String,
          let snapshot = displaySnapshots[displayID],
          snapshot.id == snapshotID,
          Date().timeIntervalSince(snapshot.created) < 45 else { throw HelperFailure("stale_display_snapshot") }
    let current = try displayGeometry(snapshot.cgID)
    guard current.sourceWidth == snapshot.sourceWidth,
          current.sourceHeight == snapshot.sourceHeight,
          current.bounds == snapshot.bounds,
          abs(CGDisplayRotation(snapshot.cgID)) < 0.01 else { throw HelperFailure("stale_display_snapshot") }
    guard CGPreflightPostEventAccess() else { throw HelperFailure("event_post_permission_required") }
    return (displayID, snapshot, current)
}

func sourcePoint(_ args: [String: Any], snapshot: DisplaySnapshot, current: DisplayRecord, xKey: String = "x", yKey: String = "y") throws -> (Int, Int, CGPoint) {
    guard let x = args[xKey] as? Int, let y = args[yKey] as? Int,
          x >= 0, y >= 0, x < snapshot.sourceWidth, y < snapshot.sourceHeight else {
        throw HelperFailure("pointer_coordinates_invalid")
    }
    return (
        x,
        y,
        CGPoint(
            x: current.bounds.origin.x + (Double(x) / Double(current.sourceWidth)) * current.bounds.width,
            y: current.bounds.origin.y + (Double(y) / Double(current.sourceHeight)) * current.bounds.height
        )
    )
}

func requireNeutralPointerState() throws {
    if CGEventSource.buttonState(.combinedSessionState, button: .left) ||
       CGEventSource.buttonState(.combinedSessionState, button: .right) ||
       CGEventSource.buttonState(.combinedSessionState, button: .center) {
        throw HelperFailure("pointer_buttons_held")
    }
    let flags = CGEventSource.flagsState(.combinedSessionState)
    let blocked: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand]
    guard flags.intersection(blocked).isEmpty else { throw HelperFailure("modifier_keys_held") }
}

func spendDisplaySnapshot(_ displayID: String) {
    displaySnapshots.removeValue(forKey: displayID)
    snapshots.removeAll()
}

func pointer(_ args: [String: Any], click: Bool) throws -> [String: Any] {
    let (displayID, snapshot, current) = try displaySnapshotContext(args)
    let (x, y, target) = try sourcePoint(args, snapshot: snapshot, current: current)
    try requireNeutralPointerState()

    let buttonName = args["button"] as? String ?? "left"
    let clickCount = args["click_count"] as? Int ?? 1
    let mouseButton: CGMouseButton
    let downType: CGEventType
    let upType: CGEventType
    switch buttonName {
    case "left":
        mouseButton = .left; downType = .leftMouseDown; upType = .leftMouseUp
    case "right":
        mouseButton = .right; downType = .rightMouseDown; upType = .rightMouseUp
    default:
        throw HelperFailure("pointer_button_invalid")
    }
    if click && ![1, 2].contains(clickCount) { throw HelperFailure("pointer_click_count_invalid") }

    guard let source = CGEventSource(stateID: .combinedSessionState),
          let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: target, mouseButton: .left) else {
        throw HelperFailure("pointer_event_unavailable")
    }

    var clicks: [(CGEvent, CGEvent)] = []
    if click {
        for index in 1...clickCount {
            guard let down = CGEvent(mouseEventSource: source, mouseType: downType, mouseCursorPosition: target, mouseButton: mouseButton),
                  let up = CGEvent(mouseEventSource: source, mouseType: upType, mouseCursorPosition: target, mouseButton: mouseButton) else {
                throw HelperFailure("pointer_event_unavailable")
            }
            down.setIntegerValueField(.mouseEventClickState, value: Int64(index))
            up.setIntegerValueField(.mouseEventClickState, value: Int64(index))
            clicks.append((down, up))
        }
    }

    // Spend the screenshot fence before the first native post. Never replay this effect.
    spendDisplaySnapshot(displayID)
    move.post(tap: .cghidEventTap)
    guard cursorReached(target) else { throw HelperFailure("pointer_move_unverified", effect: "outcome_unknown") }

    if click {
        for (down, up) in clicks {
            guard !CGEventSource.buttonState(.combinedSessionState, button: mouseButton) else {
                throw HelperFailure("pointer_state_changed", effect: "outcome_unknown")
            }
            down.post(tap: .cghidEventTap)
            up.post(tap: .cghidEventTap)
            usleep(25_000)
        }
        guard cursorReached(target), !CGEventSource.buttonState(.combinedSessionState, button: mouseButton) else {
            throw HelperFailure("pointer_click_unverified", effect: "outcome_unknown")
        }
    }
    return [
        "success": true,
        "effect": "completed",
        "display_id": displayID,
        "x": x,
        "y": y,
        "button": buttonName,
        "click_count": click ? clickCount : 0,
        "action": click ? "click" : "move",
        "observe_again": true,
    ]
}

func pointerDrag(_ args: [String: Any]) throws -> [String: Any] {
    let (displayID, snapshot, current) = try displaySnapshotContext(args)
    let (fromX, fromY, start) = try sourcePoint(args, snapshot: snapshot, current: current, xKey: "from_x", yKey: "from_y")
    let (toX, toY, end) = try sourcePoint(args, snapshot: snapshot, current: current, xKey: "to_x", yKey: "to_y")
    try requireNeutralPointerState()
    guard let source = CGEventSource(stateID: .combinedSessionState),
          let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: start, mouseButton: .left),
          let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left),
          let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: end, mouseButton: .left) else {
        throw HelperFailure("pointer_event_unavailable")
    }
    var drags: [CGEvent] = []
    for step in 1...8 {
        let ratio = Double(step) / 8.0
        let point = CGPoint(x: start.x + (end.x - start.x) * ratio, y: start.y + (end.y - start.y) * ratio)
        guard let drag = CGEvent(mouseEventSource: source, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left) else {
            throw HelperFailure("pointer_event_unavailable")
        }
        drags.append(drag)
    }

    spendDisplaySnapshot(displayID)
    move.post(tap: .cghidEventTap)
    guard cursorReached(start) else { throw HelperFailure("pointer_move_unverified", effect: "outcome_unknown") }
    down.post(tap: .cghidEventTap)
    for drag in drags {
        drag.post(tap: .cghidEventTap)
        usleep(12_000)
    }
    up.post(tap: .cghidEventTap)
    usleep(20_000)
    guard cursorReached(end), !CGEventSource.buttonState(.combinedSessionState, button: .left) else {
        throw HelperFailure("pointer_drag_unverified", effect: "outcome_unknown")
    }
    return [
        "success": true, "effect": "completed", "display_id": displayID,
        "from_x": fromX, "from_y": fromY, "to_x": toX, "to_y": toY,
        "action": "drag", "observe_again": true,
    ]
}

func pointerScroll(_ args: [String: Any]) throws -> [String: Any] {
    let (displayID, snapshot, current) = try displaySnapshotContext(args)
    let (x, y, target) = try sourcePoint(args, snapshot: snapshot, current: current)
    try requireNeutralPointerState()
    guard let deltaY = args["delta_y"] as? Int, let deltaX = args["delta_x"] as? Int,
          (deltaY != 0 || deltaX != 0), abs(deltaY) <= 120, abs(deltaX) <= 120 else {
        throw HelperFailure("scroll_delta_invalid")
    }
    guard let source = CGEventSource(stateID: .combinedSessionState),
          let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: target, mouseButton: .left),
          let scroll = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2, wheel1: Int32(deltaY), wheel2: Int32(deltaX), wheel3: 0) else {
        throw HelperFailure("scroll_event_unavailable")
    }

    spendDisplaySnapshot(displayID)
    move.post(tap: .cghidEventTap)
    guard cursorReached(target) else { throw HelperFailure("pointer_move_unverified", effect: "outcome_unknown") }
    scroll.post(tap: .cghidEventTap)
    return [
        "success": true, "effect": "completed", "display_id": displayID,
        "x": x, "y": y, "delta_y": deltaY, "delta_x": deltaX,
        "action": "scroll", "observe_again": true,
    ]
}

func click(_ args: [String: Any]) throws -> [String: Any] {
    guard let id = args["element_id"] as? String else { throw HelperFailure("element_id_required") }
    let (snapshot, optional) = try requireSnapshot(args, elementID: id)
    guard let element = optional else { throw HelperFailure("stale_element") }
    if let enabled = value(element, kAXEnabledAttribute as CFString) as? NSNumber, !enabled.boolValue { throw HelperFailure("element_disabled") }
    var actions: CFArray?
    guard AXUIElementCopyActionNames(element, &actions) == .success,
          (actions as? [String] ?? []).contains(kAXPressAction as String) else { throw HelperFailure("press_unsupported") }
    let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
    snapshots.removeValue(forKey: "\(snapshot.appID):\(snapshot.pid)")
    guard result == .success else { throw HelperFailure("press_failed_\(result.rawValue)", effect: "outcome_unknown") }
    return ["success": true, "effect": "completed", "observe_again": true]
}

func typeText(_ args: [String: Any]) throws -> [String: Any] {
    guard let id = args["element_id"] as? String, let text = args["text"] as? String,
          text.count <= 20_000, args["mode"] as? String == "replace_value" else { throw HelperFailure("text_arguments_invalid") }
    let (snapshot, optional) = try requireSnapshot(args, elementID: id)
    guard let element = optional else { throw HelperFailure("stale_element") }
    let role = string(element, kAXRoleAttribute as CFString) ?? ""
    guard ["AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"].contains(role) else { throw HelperFailure("text_role_unsupported") }
    var writable: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &writable) == .success, writable.boolValue else { throw HelperFailure("text_not_writable") }
    let result = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, text as CFTypeRef)
    snapshots.removeValue(forKey: "\(snapshot.appID):\(snapshot.pid)")
    guard result == .success else { throw HelperFailure("text_write_failed_\(result.rawValue)", effect: "outcome_unknown") }
    return ["success": true, "effect": "completed", "strategy": "AXValue", "observe_again": true]
}

func typeKeyboard(_ args: [String: Any]) throws -> [String: Any] {
    guard let text = args["text"] as? String, !text.isEmpty, text.count <= 4_000 else {
        throw HelperFailure("keyboard_text_invalid")
    }
    let (snapshot, _) = try requireSnapshot(args)
    let (_, running, app) = try targetApp(args)
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == running.processIdentifier,
          let focused = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?,
          axSame(focused, snapshot.window) else { throw HelperFailure("window_not_focused") }
    guard CGPreflightPostEventAccess() else { throw HelperFailure("event_post_permission_required") }
    guard let source = CGEventSource(stateID: .combinedSessionState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
        throw HelperFailure("key_event_unavailable")
    }

    let unicode = Array(text.utf16)
    unicode.withUnsafeBufferPointer { buffer in
        down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
        up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
    }
    snapshots.removeValue(forKey: "\(snapshot.appID):\(snapshot.pid)")
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return ["success": true, "effect": "completed", "strategy": "QuartzUnicode", "observe_again": true]
}

func pressKey(_ args: [String: Any]) throws -> [String: Any] {
    let (snapshot, _) = try requireSnapshot(args)
    let (_, running, app) = try targetApp(args)
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == running.processIdentifier,
          let focused = value(app, kAXFocusedWindowAttribute as CFString) as! AXUIElement?,
          axSame(focused, snapshot.window) else { throw HelperFailure("window_not_focused") }
    let codes: [String: CGKeyCode] = [
        "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9,
        "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
        "O": 31, "U": 32, "I": 34, "P": 35, "L": 37, "J": 38, "K": 40, "N": 45, "M": 46,
        "Return": 36, "Tab": 48, "Space": 49, "Backspace": 51, "Escape": 53,
        "Home": 115, "PageUp": 116, "DeleteForward": 117, "End": 119, "PageDown": 121,
        "Left": 123, "Right": 124, "Down": 125, "Up": 126,
    ]
    guard let key = args["key"] as? String, let code = codes[key] else { throw HelperFailure("key_not_allowed") }
    let modifiers = args["modifiers"] as? [String] ?? []
    let allowedModifiers = Set(["shift", "option", "control", "command"])
    guard modifiers.count <= 4, Set(modifiers).count == modifiers.count,
          modifiers.allSatisfy({ allowedModifiers.contains($0) }) else { throw HelperFailure("modifiers_invalid") }
    var flags: CGEventFlags = []
    for modifier in modifiers {
        switch modifier {
        case "shift": flags.insert(.maskShift)
        case "option": flags.insert(.maskAlternate)
        case "control": flags.insert(.maskControl)
        case "command": flags.insert(.maskCommand)
        default: break
        }
    }
    guard CGPreflightPostEventAccess() else { throw HelperFailure("event_post_permission_required") }
    guard let source = CGEventSource(stateID: .combinedSessionState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else { throw HelperFailure("key_event_unavailable") }
    down.flags = flags; up.flags = flags
    // Spend the window snapshot before the first native post. Never replay an uncertain key chord.
    snapshots.removeValue(forKey: "\(snapshot.appID):\(snapshot.pid)")
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return ["success": true, "effect": "completed", "key": key, "modifiers": modifiers, "observe_again": true]
}

func handle(_ input: [String: Any]) throws -> [String: Any] {
    guard let operation = input["operation"] as? String, let args = input["args"] as? [String: Any] else { throw HelperFailure("invalid_request") }
    switch operation {
    case "list_apps":
        let allowlist = try allowed(args)
        var seen = Set<pid_t>()
        let candidates = allowlist.contains("*")
            ? NSWorkspace.shared.runningApplications
            : allowlist.flatMap { NSRunningApplication.runningApplications(withBundleIdentifier: $0) }
        let running = candidates
            .filter { !$0.isTerminated && seen.insert($0.processIdentifier).inserted }
            .filter { $0.bundleIdentifier != nil }
            .sorted { $0.processIdentifier < $1.processIdentifier }
        let limit = max(1, min(args["limit"] as? Int ?? 32, 64))
        return ["apps": Array(running.prefix(limit)).map { ["app_id": $0.bundleIdentifier ?? "", "name": $0.localizedName ?? "", "pid": Int($0.processIdentifier)] }, "accessibility_trusted": AXIsProcessTrusted(), "truncated": running.count > limit]
    case "get_state": return try state(args)
    case "screenshot": return try screenshot(args)
    case "permission_status": return try permissionStatus(args)
    case "request_permissions": return try requestPermissions(args)
    case "list_displays": return try listDisplays(args)
    case "screenshot_display": return try screenshotDisplay(args)
    case "launch_app": return try launchApp(args)
    case "list_windows": return try listWindows(args)
    case "select_window_background": return try selectWindowBackground(args)
    case "activate_window": return try activateWindow(args)
    case "pointer_move": return try pointer(args, click: false)
    case "pointer_click": return try pointer(args, click: true)
    case "pointer_drag": return try pointerDrag(args)
    case "pointer_scroll": return try pointerScroll(args)
    case "click": return try click(args)
    case "press_key": return try pressKey(args)
    case "type_text": return try typeText(args)
    case "type_keyboard": return try typeKeyboard(args)
    default: throw HelperFailure("unknown_operation")
    }
}

let appHost = NSApplication.shared
appHost.setActivationPolicy(.prohibited)
while let line = readLine(strippingNewline: true) {
    let requestID: String
    var response: [String: Any]
    do {
        guard line.utf8.count <= 100_000,
              let data = line.data(using: .utf8),
              let input = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = input["request_id"] as? String else { throw HelperFailure("invalid_request") }
        requestID = id
        response = ["request_id": requestID, "ok": true, "result": try handle(input)]
    } catch let selection as SelectionRequired {
        response = ["request_id": (try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])?["request_id"] as? String ?? "", "ok": true, "result": selection.result]
    } catch let failure as HelperFailure {
        response = ["request_id": (try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])?["request_id"] as? String ?? "", "ok": false, "error": failure.code, "effect": failure.effect]
    } catch {
        response = ["request_id": "", "ok": false, "error": "invalid_request"]
    }
    if let data = try? JSONSerialization.data(withJSONObject: response, options: [.fragmentsAllowed]),
       let output = String(data: data, encoding: .utf8) { print(output); fflush(stdout) }
}
