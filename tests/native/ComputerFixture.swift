import AppKit

final class Fixture: NSObject {
    var window: NSWindow!
    var button: NSButton!
    var container: NSStackView!
    var count = 0
    var secondWindow: NSWindow?

    func start() {
        window = NSWindow(contentRect: NSRect(x: 250, y: 250, width: 440, height: 230), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "MCP4ChatGPT Computer Fixture"
        container = NSStackView(frame: NSRect(x: 20, y: 50, width: 400, height: 140))
        container.orientation = .vertical
        button = NSButton(title: "Increment 0", target: self, action: #selector(increment))
        button.bezelStyle = .rounded
        let field = NSTextField(string: "fixture text")
        container.addArrangedSubview(button)
        container.addArrangedSubview(field)
        window.contentView?.addSubview(container)
        window.makeKeyAndOrderFront(nil)
        if CommandLine.arguments.contains("--two-windows") {
            let other = NSWindow(contentRect: NSRect(x: 750, y: 250, width: 440, height: 230), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            other.title = CommandLine.arguments.contains("--duplicate-titles") ? window.title : "MCP4ChatGPT Second Window"
            let text = NSTextField(string: "second window")
            text.frame = NSRect(x: 20, y: 70, width: 300, height: 30)
            other.contentView?.addSubview(text)
            other.makeKeyAndOrderFront(nil)
            secondWindow = other
            if CommandLine.arguments.contains("--minimize-second") { other.miniaturize(nil) }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func increment() {
        count += 1
        button.title = "Increment \(count)"
        let label = NSTextField(labelWithString: "Inserted \(count)")
        container.insertArrangedSubview(label, at: 0)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
app.finishLaunching()
let fixture = Fixture()
fixture.start()
app.run()
