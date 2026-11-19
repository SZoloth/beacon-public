import Foundation
import Testing
@testable import BeaconTriage

/// The local server reads the default checkout's database. Pointing the app at another checkout
/// must stop reads from going to that server while writes go to the other checkout's database.
@Suite struct BeaconCLITests {
    @Test func unsetOrDefaultCheckoutUsesTheLocalServer() {
        #expect(BeaconCLI.sharesServer(repoOverride: nil))
        #expect(BeaconCLI.sharesServer(repoOverride: BeaconCLI.defaultRepoPath))
        #expect(BeaconCLI.sharesServer(repoOverride: NSString(string: BeaconCLI.defaultRepoPath).expandingTildeInPath))
    }

    @Test func anyOtherCheckoutBypassesTheLocalServer() {
        #expect(!BeaconCLI.sharesServer(repoOverride: "/tmp/beacon-scratch"))
        #expect(!BeaconCLI.sharesServer(repoOverride: "~/Projects/beacon-copy"))
    }

    // B08: a command must never wedge the app's CLI actor.

    private static let shell = URL(fileURLWithPath: "/bin/sh")

    /// 1 MB of errors is far more than a pipe holds; reading only stdout first would wait forever.
    @Test(.timeLimit(.minutes(1))) func aCommandThatFillsItsErrorPipeStillFinishes() throws {
        let data = try BeaconCLI.run(Self.shell, ["-c", "head -c 1048576 /dev/zero >&2; echo done"], timeout: 30)
        #expect(String(decoding: data, as: UTF8.self) == "done\n")
    }

    @Test(.timeLimit(.minutes(1))) func aFailingCommandReportsItsErrors() throws {
        #expect(throws: BeaconCLI.Failure.self) {
            try BeaconCLI.run(Self.shell, ["-c", "echo nope >&2; exit 3"], timeout: 30)
        }
    }

    @Test(.timeLimit(.minutes(1))) func aCommandThatNeverExitsIsStoppedAtItsDeadline() throws {
        let start = Date()
        do {
            _ = try BeaconCLI.run(Self.shell, ["-c", "sleep 300"], timeout: 1)
            Issue.record("expected a timeout")
        } catch let failure as BeaconCLI.Failure {
            guard case .timedOut = failure else { Issue.record("expected a timeout, got \(failure)"); return }
        }
        #expect(Date().timeIntervalSince(start) < 20)
    }

    /// The planning agent is a child of Python. Stopping only Python left it running with the pipes.
    @Test(.timeLimit(.minutes(1)), arguments: [
        "sleep 300 & echo $! > \"$0\"; wait",
        "(trap '' TERM; exec sleep 300) & echo $! > \"$0\"; wait",
    ])
    func aTimeoutAlsoStopsWhatTheCommandStarted(script: String) throws {
        let pidFile = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        var child: pid_t = 0
        defer {
            if child > 0 { kill(child, SIGKILL) }
            try? FileManager.default.removeItem(at: pidFile)
        }
        #expect(throws: BeaconCLI.Failure.self) {
            try BeaconCLI.run(Self.shell, ["-c", script, pidFile.path], timeout: 1)
        }
        child = pid_t((try? String(contentsOf: pidFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? 0
        try #require(child > 0)
        let start = Date()
        while kill(child, 0) == 0, Date().timeIntervalSince(start) < 5 { usleep(50_000) }
        #expect(kill(child, 0) == -1 && errno == ESRCH, "the command's child \(child) is still running")
    }

    /// Python's own timeouts, read from lib/. The app must wait longer than each, or a save that
    /// landed (keep commits before it indexes) shows as failed.
    private static let pythonTimeouts: [([String], TimeInterval)] = [
        (["keep", "--url", "u"], 60 + 15 + 30 * 60),  // bird read, page fetch (lib/adds.py), indexing (lib/vectors.py)
        (["brief", "--project", "p"], 15 * 60),      // planning agent (lib/briefs.py)
        (["compose", "layout"], 120 + 60),           // meaning search (lib/vectors.py), layout model (lib/compose.py)
        (["references", "--brief=b"], 120 + 20),     // meaning search, Jev (lib/useful.py)
        (["library", "--offset", "0"], 120),         // meaning search
        (["mark", "adopted", "1"], 0),
        (["activate", "1"], 15 * 60 + 45 * 60),            // git check, then the agent's run (lib/activate.py)
        (["activity-answer", "1", "--", "a"], 15 * 60 + 45 * 60),
    ]

    @Test(arguments: pythonTimeouts)
    func eachCommandOutwaitsPythonsOwnTimeout(arguments: [String], python: TimeInterval) {
        #expect(BeaconCLI.deadline(for: arguments) > python, "\(arguments[0])")
    }

    @Test func aRefusalShowsBeaconsReasonNotTheExitStatus() async {
        await #expect(throws: BeaconCLI.Failure.self) {
            try await BeaconCLI.inBeaconsWords { () async throws -> Int in
                throw BeaconCLI.Failure.exited(2, "activate: Folio has client folders, so Beacon never hands saves there.\n")
            }
        }
        do {
            _ = try await BeaconCLI.inBeaconsWords { () async throws -> Int in
                throw BeaconCLI.Failure.exited(2, "activate: Folio has client folders, so Beacon never hands saves there.\n")
            }
        } catch {
            #expect(error.localizedDescription == "Folio has client folders, so Beacon never hands saves there.")
        }
        do {
            _ = try await BeaconCLI.inBeaconsWords { () async throws -> Int in
                throw BeaconCLI.Failure.exited(1, "Only 0 saves are scored on why. Run `beacon pick score --axis why` first.\n")
            }
        } catch {
            #expect(error.localizedDescription == "Only 0 saves are scored on why. Run `beacon pick score --axis why` first.")
        }
    }
}
