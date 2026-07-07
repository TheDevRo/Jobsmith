import Foundation
import ZIPFoundation

/// Packages document.xml (+ relationships) into an OPC .docx zip container.
enum DocxArchive {
    static let contentTypesXML = """
    <?xml version="1.0" encoding="UTF-8" standalone="yes"?>\
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\
    <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\
    <Default Extension="xml" ContentType="application/xml"/>\
    <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\
    </Types>
    """

    static let rootRelsXML = """
    <?xml version="1.0" encoding="UTF-8" standalone="yes"?>\
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\
    <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\
    </Relationships>
    """

    static func package(documentXML: String, documentRelsXML: String) throws -> Data {
        guard let archive = Archive(accessMode: .create) else {
            throw DocxError.archiveCreationFailed
        }
        let entries: [(String, String)] = [
            ("[Content_Types].xml", contentTypesXML),
            ("_rels/.rels", rootRelsXML),
            ("word/document.xml", documentXML),
            ("word/_rels/document.xml.rels", documentRelsXML),
        ]
        for (path, content) in entries {
            let data = Data(content.utf8)
            try archive.addEntry(with: path, type: .file,
                                 uncompressedSize: Int64(data.count),
                                 provider: { position, size in
                data.subdata(in: Int(position)..<Int(position) + size)
            })
        }
        guard let data = archive.data else {
            throw DocxError.archiveCreationFailed
        }
        return data
    }

    enum DocxError: Error {
        case archiveCreationFailed
    }
}
