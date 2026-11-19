import QuartzCore
import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

/// Speed benchmark, on only when the app starts with BEACON_BENCH=1 (see scripts/bench-app.sh).
///
/// Opens Library, times launch until 12 pictures show, then scrolls 2,000 saves' worth at a steady
/// speed and adds up the time lost to late frames (Apple calls this the hitch ratio; under 5 ms per
/// second is "good"). Writes the numbers as JSON and quits. Targets: docs/plans/2026-10-09-speed-hillclimb.md.
@MainActor
enum Bench {
    static let enabled = ProcessInfo.processInfo.environment["BEACON_BENCH"] != nil
    /// BEACON_BENCH=switch: skip the scroll and only time tab switches.
    static let switchesOnly = ProcessInfo.processInfo.environment["BEACON_BENCH"] == "switch"
    /// BEACON_BENCH=plain: gray boxes instead of tiles, to measure what the Mac alone costs.
    static let plainTiles = ProcessInfo.processInfo.environment["BEACON_BENCH"] == "plain"
    static let output: URL = {
        #if os(macOS)
        URL(fileURLWithPath: "/tmp/beacon-bench.json")
        #else
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("beacon-bench.json")
        #endif
    }()
    private static var picturesShown = 0
    private static var results: [String: Any] = [:]
    static var firstPicturesShown = false

    static func start() {
        guard enabled else { return }
        UserDefaults.standard.set(AppTab.library.rawValue, forKey: "tab")
        if plainTiles {  // no pictures to wait for; give the first page 3 seconds to load
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { firstPicturesShown = true }
        }
    }

    /// The first page of saves arrived from the server.
    static func firstPage() { mark("launch_to_first_page_ms") }

    /// Time since launch, recorded once under this name.
    static func mark(_ name: String) {
        guard enabled, results[name] == nil else { return }
        results[name] = Int((Date().timeIntervalSince1970 - processStart) * 1000)
    }

    /// A tile's picture appeared.
    static func pictureShown() {
        guard enabled, !firstPicturesShown else { return }
        picturesShown += 1
        if picturesShown == 12 {
            firstPicturesShown = true
            if switchesOnly { scrollDone = true }
            results["launch_to_12_pictures_ms"] = Int((Date().timeIntervalSince1970 - processStart) * 1000)
        }
    }

    /// Set when the scroll is done; then BenchSwitcher switches tabs.
    static var scrollDone = false

    static func finish(scrolled seconds: Double, hitch: Double, frames: Int, worstFrameMs: Double) {
        results["scroll_seconds"] = (seconds * 10).rounded() / 10
        results["hitch_ms_per_s"] = (hitch / seconds * 10).rounded() / 10
        results["frames"] = frames
        results["worst_frame_ms"] = Int(worstFrameMs)
        scrollDone = true
    }

    /// Time lost to late frames in the half second after switching to each tab (median of 3 rounds).
    static func finishSwitches(_ lost: [AppTab: [Double]]) {
        for (tab, values) in lost {
            results["switch_to_\(tab.rawValue.lowercased())_ms"] = Int(values.sorted()[values.count / 2])
        }
        if let data = try? JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]) {
            try? data.write(to: output)
        }
        exit(0)
    }

    /// When this process started, from the kernel (before any of our code ran).
    private static let processStart: Double = {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var name: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
        sysctl(&name, 4, &info, &size, nil, 0)
        let start = info.kp_proc.p_un.__p_starttime
        return Double(start.tv_sec) + Double(start.tv_usec) / 1_000_000
    }()
}

#if os(macOS)
typealias PlatformView = NSView
typealias PlatformScrollView = NSScrollView
#else
typealias PlatformView = UIView
typealias PlatformScrollView = UIScrollView
#endif

/// Put behind the Library's scroll content: finds its scroll view and, once the first pictures show,
/// scrolls it at a steady 2,400 points a second while a display link times every frame.
final class BenchScroller: PlatformView {
    private var link: CADisplayLink?
    private var last: CFTimeInterval = 0
    private var began: CFTimeInterval = 0
    private var hitch: Double = 0
    private var frames = 0
    private var worst: Double = 0
    private weak var scrollView: PlatformScrollView?
    private let speed: CGFloat = 2400
    private let duration: CFTimeInterval = 12

    #if os(macOS)
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, link == nil else { return }
        link = displayLink(target: self, selector: #selector(tick))
        link?.add(to: .main, forMode: .common)
    }
    #else
    override func didMoveToWindow() {
        super.didMoveToWindow()
        guard window != nil, link == nil else { return }
        link = CADisplayLink(target: self, selector: #selector(tick))
        link?.add(to: .main, forMode: .common)
    }
    #endif

    @objc private func tick(_ link: CADisplayLink) {
        MainActor.assumeIsolated {
            guard Bench.firstPicturesShown, !Bench.switchesOnly else { return }
            if scrollView == nil { scrollView = findScrollView() }
            guard let scrollView else { return }
            let now = link.timestamp
            let expected = link.targetTimestamp - link.timestamp
            if began == 0 { began = now; last = now; return }
            let interval = now - last
            frames += 1
            worst = max(worst, interval * 1000)
            // A frame that arrives later than one refresh plus a little slack is a hitch.
            if interval > expected * 1.5 { hitch += (interval - expected) * 1000 }
            last = now
            move(scrollView, by: speed * CGFloat(interval))
            if now - began >= duration {
                link.invalidate()
                Bench.finish(scrolled: now - began, hitch: hitch, frames: frames, worstFrameMs: worst)
            }
        }
    }

    private func findScrollView() -> PlatformScrollView? {
        var view: PlatformView? = superview
        while let current = view {
            if let scroll = current as? PlatformScrollView { return scroll }
            view = current.superview
        }
        return nil
    }

    private func move(_ scrollView: PlatformScrollView, by points: CGFloat) {
        #if os(macOS)
        let clip = scrollView.contentView
        let maxY = max(0, (scrollView.documentView?.frame.height ?? 0) - clip.bounds.height)
        clip.scroll(to: NSPoint(x: 0, y: min(clip.bounds.origin.y + points, maxY)))
        scrollView.reflectScrolledClipView(clip)
        #else
        let maxY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        scrollView.contentOffset.y = min(scrollView.contentOffset.y + points, maxY)
        #endif
    }
}

#if os(macOS)
struct BenchScrollDriver: NSViewRepresentable {
    func makeNSView(context: Context) -> BenchScroller { BenchScroller() }
    func updateNSView(_ view: BenchScroller, context: Context) {}
}
#else
struct BenchScrollDriver: UIViewRepresentable {
    func makeUIView(context: Context) -> BenchScroller { BenchScroller() }
    func updateUIView(_ view: BenchScroller, context: Context) {}
}
#endif

/// At the app's root: after the scroll, switches Triage, Briefs, Library three times, half a second
/// apart, and adds up the late-frame time after each switch (the work a switch costs).
final class BenchSwitcher: PlatformView {
    private var link: CADisplayLink?
    private var last: CFTimeInterval = 0
    private var plan: [AppTab] = Array(repeating: [AppTab.triage, .activity, .library], count: 3).flatMap { $0 }
    private var current: AppTab?
    private var framesSince = 0
    private var lostSince = 0.0
    private var lost: [AppTab: [Double]] = [:]

    #if os(macOS)
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, link == nil else { return }
        link = displayLink(target: self, selector: #selector(tick))
        link?.add(to: .main, forMode: .common)
    }
    #else
    override func didMoveToWindow() {
        super.didMoveToWindow()
        guard window != nil, link == nil else { return }
        link = CADisplayLink(target: self, selector: #selector(tick))
        link?.add(to: .main, forMode: .common)
    }
    #endif

    @objc private func tick(_ link: CADisplayLink) {
        MainActor.assumeIsolated {
            let now = link.timestamp, expected = link.targetTimestamp - link.timestamp
            defer { last = now }
            guard Bench.scrollDone, last > 0 else { return }
            let interval = now - last
            if current != nil {
                framesSince += 1
                if interval > expected * 1.5 { lostSince += (interval - expected) * 1000 }
            }
            guard current == nil || framesSince >= 30 else { return }
            if let current { lost[current, default: []].append(lostSince) }
            guard !plan.isEmpty else {
                link.invalidate()
                Bench.finishSwitches(lost)
                return
            }
            let next = plan.removeFirst()
            current = next
            framesSince = 0
            lostSince = 0
            // The way a click does it (TabPicker), animation included.
            withAnimation(.spring(duration: 0.32, bounce: 0.2)) { UserDefaults.standard.set(next.rawValue, forKey: "tab") }
        }
    }
}

#if os(macOS)
struct BenchSwitchDriver: NSViewRepresentable {
    func makeNSView(context: Context) -> BenchSwitcher { BenchSwitcher() }
    func updateNSView(_ view: BenchSwitcher, context: Context) {}
}
#else
struct BenchSwitchDriver: UIViewRepresentable {
    func makeUIView(context: Context) -> BenchSwitcher { BenchSwitcher() }
    func updateUIView(_ view: BenchSwitcher, context: Context) {}
}
#endif
