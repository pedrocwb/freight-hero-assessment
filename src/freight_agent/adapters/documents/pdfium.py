"""The production `PageReader`, on `pypdfium2` (Apache/BSD; not PyMuPDF, which is AGPL):
each page's text, and the page rendered as a PNG when its text is too thin to trust.

Owner: card W3-DOC (stub from C0).

Contract with the composition root: define `PdfiumPageReader()` here, under that name.
`bootstrap.build_documents` builds the pipeline once both it and
`application.documents.DocumentPipeline` exist; until then the runtime runs
`NullDocumentProcessor`.
"""
