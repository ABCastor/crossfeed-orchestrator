import AppKit
import XCTest
import CrossfeedCore
@testable import CrossfeedUI

final class Round12cUITests: XCTestCase {
    func testTheThemeSwitchHasOneTemplateIconPerChoiceAndTheyDiffer() throws {
        var seen = Set<Data>()
        for look in PanelPrefs.Appearance.allCases {
            let image = try XCTUnwrap(ToolIcons.image(for: look), "\(look) icon")
            XCTAssertTrue(image.isTemplate, "\(look) is drawn as a template")
            XCTAssertEqual(image.size.width, image.size.height)
            seen.insert(try XCTUnwrap(image.tiffRepresentation))
        }
        XCTAssertEqual(seen.count, 3)
    }

    func testTheIconsAreTheCastorMasterDrawings() {
        XCTAssertTrue(ToolIcons.sunSVG.contains(#"<g class="t-sun">"#))
        XCTAssertTrue(ToolIcons.moonSVG.contains(#"<g class="t-moon">"#))
        XCTAssertTrue(ToolIcons.systemSVG.contains(#"<g class="t-system">"#))
        for svg in [ToolIcons.sunSVG, ToolIcons.moonSVG, ToolIcons.systemSVG] {
            XCTAssertTrue(svg.contains(#"stroke="currentColor""#) && svg.contains(#"viewBox="0 0 24 24""#))
        }
    }
}
