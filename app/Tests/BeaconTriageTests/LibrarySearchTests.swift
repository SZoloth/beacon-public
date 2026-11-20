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

}
