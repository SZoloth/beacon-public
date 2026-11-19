import Foundation
import Testing
@testable import BeaconTriage

struct LibrarySearchTests {
    @Test func oneBoxSearchPassesEachStepToTheCLIAndServer() {
        let search = LibrarySearch.search("chairs", refine: ["wooden", "-tall"])
        #expect(search.arguments == ["--search=chairs", "--refine=wooden", "--refine=-tall"])
        #expect(search.queryItems.map(\.name) == ["search", "refine", "refine"])
        #expect(search.queryItems.map(\.value) == ["chairs", "wooden", "-tall"])
    }

    @Test func aSearchPageCarriesWhatFoundEachSave() throws {
        let page = try JSONDecoder().decode(LibraryPage.self, from: Data("""
        {"total": 2, "next_offset": null, "searched": ["words"], "set_aside": 3, "items": [
          {"id": 1, "source": "x", "url": null, "title": "a", "text": null, "author": null, "author_name": null,
           "repo": null, "media": [], "area": null, "found_by": ["words", "meaning"], "fit": "strong"},
          {"id": 2, "source": "github", "url": null, "title": "b", "text": null, "author": null, "author_name": null,
           "repo": {"name": "o/b", "language": null}, "media": [], "area": null}]}
        """.utf8))
        #expect(page.searched == ["words"])
        #expect(page.setAside == 3)
        #expect(page.items[0].foundBy == ["words", "meaning"])
        #expect(page.items[0].fit == "strong")
        #expect(page.items[1].fit == nil)
    }

    @Test func aLayoutDecodesAndANullLayoutMeansThePlainGrid() throws {
        let layout = try JSONDecoder().decode(ComposeResult.self, from: Data("""
        {"id": 4, "layout": {"pick": {"id": 1, "why": "w"}, "groups": [{"title": "T", "ids": [1, 2]}], "next": ["tall"]},
         "why": null, "cost_usd": 0.002, "cached": false}
        """.utf8))
        #expect(layout.layout?.groups.first?.ids == [1, 2])
        #expect(layout.layout?.pick?.id == 1)
        let none = try JSONDecoder().decode(ComposeResult.self, from: Data("""
        {"id": null, "layout": null, "why": "too few saves to group", "cost_usd": 0.0, "cached": false}
        """.utf8))
        #expect(none.layout == nil && none.id == nil)
    }
}
